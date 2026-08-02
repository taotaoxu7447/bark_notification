package io.github.taotaoxu7447.agentwatch

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationChannelGroup
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.media.AudioAttributes

class NotificationRenderer(private val context: Context) {
    private val manager = context.getSystemService(NotificationManager::class.java)

    fun eventBlockReason(source: NtfyMessage.Source, silentRecovery: Boolean): String? {
        if (!manager.areNotificationsEnabled()) return "系统通知总开关已关闭，本条未标记为送达"
        val primaryChannel = manager.getNotificationChannel(SourcePresentation.channelId(source))
        if (primaryChannel == null || primaryChannel.importance == NotificationManager.IMPORTANCE_NONE) {
            return "${source.displayName} 通知频道已关闭，本条未标记为送达"
        }
        if (silentRecovery) {
            val recoveryChannel = manager.getNotificationChannel(SourcePresentation.recoveryChannelId(source))
            if (recoveryChannel == null || recoveryChannel.importance == NotificationManager.IMPORTANCE_NONE) {
                return "${source.displayName} 静默恢复频道已关闭，本条未标记为送达"
            }
        }
        return null
    }

    fun createChannels() {
        manager.createNotificationChannelGroup(
            NotificationChannelGroup(SOURCE_CHANNEL_GROUP, context.getString(R.string.source_group_name)),
        )
        val connection = NotificationChannel(
            CONNECTION_CHANNEL,
            context.getString(R.string.connection_channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = context.getString(R.string.connection_channel_description)
            setSound(null, null)
            enableVibration(false)
            setShowBadge(false)
        }
        manager.createNotificationChannel(connection)

        NtfyMessage.Source.entries.forEach { source ->
            val channel = NotificationChannel(
                SourcePresentation.channelId(source),
                source.displayName,
                NotificationManager.IMPORTANCE_HIGH,
            ).apply {
                group = SOURCE_CHANNEL_GROUP
                description = "${source.displayName} 任务完成与需要处理提醒"
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 220, 120, 220)
                setShowBadge(true)
                lockscreenVisibility = Notification.VISIBILITY_PRIVATE
                setSound(
                    android.provider.Settings.System.DEFAULT_NOTIFICATION_URI,
                    AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_EVENT).build(),
                )
            }
            manager.createNotificationChannel(channel)
            manager.createNotificationChannel(
                NotificationChannel(
                    SourcePresentation.recoveryChannelId(source),
                    "${source.displayName}（静默恢复）",
                    NotificationManager.IMPORTANCE_LOW,
                ).apply {
                    group = SOURCE_CHANNEL_GROUP
                    description = "仅在进程中断后的去重恢复中使用，不会再次响铃"
                    setSound(null, null)
                    enableVibration(false)
                    setShowBadge(true)
                    lockscreenVisibility = Notification.VISIBILITY_PRIVATE
                },
            )
        }
    }

    fun connectionNotification(state: String, detail: String): Notification {
        val text = when (state) {
            StatusStore.STATE_CONNECTED -> "WebSocket 已连接"
            StatusStore.STATE_CONNECTING -> "正在连接服务器"
            StatusStore.STATE_RECONNECTING -> detail.ifBlank { "连接中断，正在自动重连" }
            StatusStore.STATE_AUTH_FAILED -> "登录凭据已失效，请重新登录"
            else -> detail.ifBlank { "实时接收服务正在运行" }
        }
        return Notification.Builder(context, CONNECTION_CHANNEL)
            .setSmallIcon(R.drawable.ic_status_connection)
            .setContentTitle("AgentWatch 正在接收")
            .setContentText(text)
            .setContentIntent(mainPendingIntent())
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(Notification.CATEGORY_SERVICE)
            .setShowWhen(false)
            .build()
    }

    fun showEvent(message: NtfyMessage, silent: Boolean) {
        val title = message.title.ifBlank { "${message.source.displayName} 任务提醒" }
        val builder = Notification.Builder(
            context,
            if (silent) {
                SourcePresentation.recoveryChannelId(message.source)
            } else {
                SourcePresentation.channelId(message.source)
            },
        )
            .setSmallIcon(SourcePresentation.smallIcon(message.source))
            .setContentTitle(title.take(180))
            .setContentText(message.message.lineSequence().firstOrNull().orEmpty().take(220))
            .setStyle(Notification.BigTextStyle().bigText(message.message.take(3500)))
            .setContentIntent(mainPendingIntent(message.eventKey))
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
            .setCategory(Notification.CATEGORY_STATUS)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setGroup("agentwatch.${message.source.key}")
            .setWhen(if (message.time > 0L) message.time * 1000L else System.currentTimeMillis())
            .setShowWhen(true)

        SourcePresentation.largeIcon(message.source)
            ?.let { BitmapFactory.decodeResource(context.resources, it) }
            ?.let(builder::setLargeIcon)
        manager.notify(eventTag(message.eventKey), eventId(message.eventKey), builder.build())
    }

    fun isEventActive(eventKey: String): Boolean = try {
        manager.activeNotifications.any {
            it.tag == eventTag(eventKey) && it.id == eventId(eventKey)
        }
    } catch (_: RuntimeException) {
        false
    }

    /**
     * Completes a display interrupted by process death without persisting or
     * reconstructing the original notification body.
     */
    fun showRecovery(
        eventKey: String,
        source: NtfyMessage.Source,
        notificationTimeMillis: Long,
        history: HistoryStore.Entry? = null,
    ) {
        val notification = Notification.Builder(context, SourcePresentation.recoveryChannelId(source))
            .setSmallIcon(SourcePresentation.smallIcon(source))
            .setContentTitle(history?.title ?: "${source.displayName} 任务提醒")
            .setContentText(history?.body?.lineSequence()?.firstOrNull()?.take(220) ?: "进程中断后已静默恢复送达")
            .setStyle(history?.let { Notification.BigTextStyle().bigText(it.body.take(3500)) })
            .setContentIntent(mainPendingIntent(eventKey))
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
            .setCategory(Notification.CATEGORY_STATUS)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setGroup("agentwatch.${source.key}")
            .setWhen(notificationTimeMillis.takeIf { it > 0L } ?: System.currentTimeMillis())
            .setShowWhen(true)
            .build()
        manager.notify(eventTag(eventKey), eventId(eventKey), notification)
    }

    private fun mainPendingIntent(eventKey: String = ""): PendingIntent = PendingIntent.getActivity(
        context,
        eventKey.hashCode(),
        Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra(MainActivity.EXTRA_EVENT_ID, eventKey),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )

    private fun eventTag(eventKey: String): String = "agentwatch:$eventKey"
    private fun eventId(eventKey: String): Int = eventKey.hashCode() and Int.MAX_VALUE

    companion object {
        const val FOREGROUND_NOTIFICATION_ID = 41001
        private const val CONNECTION_CHANNEL = "agentwatch_connection_v1"
        private const val SOURCE_CHANNEL_GROUP = "agentwatch_sources"
    }
}
