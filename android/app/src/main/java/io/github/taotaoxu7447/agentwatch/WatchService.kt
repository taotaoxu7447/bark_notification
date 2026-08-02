package io.github.taotaoxu7447.agentwatch

import android.Manifest
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.random.Random

class WatchService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private val socketLock = Any()
    private lateinit var renderer: NotificationRenderer
    private lateinit var statusStore: StatusStore
    private lateinit var cursorStore: CursorStore
    private lateinit var dedupeStore: EventDedupeStore
    private lateinit var ackOutbox: AckOutbox
    private lateinit var secretStore: SecretStore
    private lateinit var registrationClient: RegistrationClient
    private lateinit var historyStore: HistoryStore
    private lateinit var historySettings: HistorySettings
    private lateinit var connectivityManager: ConnectivityManager

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(25, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private var webSocket: WebSocket? = null
    private var socketGeneration = 0L
    private var reconnectAttempt = 0
    private var reconnectRunnable: Runnable? = null
    private var ackRunnable: Runnable? = null
    private var ackInFlight = false
    private var deliveryRecoveryCompleted = false
    private var networkCallbackRegistered = false
    private var defaultNetwork: Network? = null
    private var defaultNetworkHasInternet = false
    @Volatile private var destroyed = false
    @Volatile private var stopRequested = false

    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            handler.post {
                val changed = defaultNetwork != null && defaultNetwork != network
                defaultNetwork = network
                defaultNetworkHasInternet = networkHasInternet(network)
                if (changed) {
                    reconnectAttempt = 0
                    closeSocket("default network changed")
                }
                if (defaultNetworkHasInternet) {
                    connectNow(if (changed) "network switched" else "network available")
                } else {
                    setState(StatusStore.STATE_RECONNECTING, "等待网络恢复")
                }
            }
        }

        override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
            handler.post {
                val isCurrent = defaultNetwork == network
                val hasInternet = capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                when (networkCapabilityAction(isCurrent, defaultNetworkHasInternet, hasInternet)) {
                    NetworkCapabilityAction.CONNECT -> {
                        defaultNetworkHasInternet = true
                        reconnectAttempt = 0
                        connectNow("network capability restored")
                    }
                    NetworkCapabilityAction.DISCONNECT -> {
                        defaultNetworkHasInternet = false
                        closeSocket("network capability lost")
                        setState(StatusStore.STATE_RECONNECTING, "网络已断开，恢复后自动连接")
                    }
                    NetworkCapabilityAction.NONE -> if (isCurrent) {
                        defaultNetworkHasInternet = hasInternet
                    }
                }
            }
        }

        override fun onLost(network: Network) {
            handler.post {
                if (defaultNetwork != network) return@post
                defaultNetwork = connectivityManager.activeNetwork
                defaultNetworkHasInternet = defaultNetwork?.let(::networkHasInternet) ?: false
                closeSocket("network lost")
                if (defaultNetworkHasInternet) {
                    reconnectAttempt = 0
                    connectNow("network switched")
                } else {
                    setState(StatusStore.STATE_RECONNECTING, "网络已断开，恢复后自动连接")
                }
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        renderer = NotificationRenderer(this).also { it.createChannels() }
        statusStore = StatusStore(this)
        cursorStore = CursorStore(this)
        dedupeStore = EventDedupeStore(this)
        ackOutbox = AckOutbox(this)
        secretStore = SecretStore(this)
        registrationClient = RegistrationClient(this)
        historyStore = HistoryStore(this)
        historySettings = HistorySettings(this)
        historyStore.cleanupAll(historySettings.retentionDays())
        connectivityManager = getSystemService(ConnectivityManager::class.java)
        promoteToForeground(StatusStore.STATE_CONNECTING, "正在启动")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            AppConfig.ACTION_STOP -> {
                stopRequested = true
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            AppConfig.ACTION_RECONNECT -> {
                reconnectAttempt = 0
                closeSocket("manual reconnect")
            }
        }

        if (LogoutStateStore(this).isPending()) {
            setState(StatusStore.STATE_STOPPED, "正在退出登录")
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }
        if (!notificationsAllowed()) {
            setState(StatusStore.STATE_PERMISSION_REQUIRED, "请先允许通知权限")
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }
        val session = secretStore.session()
        if (!session.isPrivate) {
            val detail = if (session.appToken.isNotBlank()) {
                "请打开 AgentWatch，将旧会话升级到私有通道"
            } else {
                "请重新登录"
            }
            setState(StatusStore.STATE_AUTH_FAILED, detail)
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }
        registerNetworkCallback()
        if (!deliveryRecoveryCompleted) {
            if (!recoverIncompleteDeliveries()) {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            deliveryRecoveryCompleted = true
        }
        flushAcknowledgements()
        connectNow("service start")
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        destroyed = true
        reconnectRunnable?.let(handler::removeCallbacks)
        reconnectRunnable = null
        ackRunnable?.let(handler::removeCallbacks)
        ackRunnable = null
        closeSocket("service destroyed")
        if (networkCallbackRegistered) {
            try {
                connectivityManager.unregisterNetworkCallback(networkCallback)
            } catch (_: Exception) {
                // Already unregistered by the OS.
            }
        }
        client.dispatcher.executorService.shutdown()
        client.connectionPool.evictAll()
        historyStore.close()
        if (stopRequested) statusStore.update(StatusStore.STATE_STOPPED, "接收服务已停止")
        super.onDestroy()
    }

    private fun notificationsAllowed(): Boolean {
        val runtimePermission = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
        return runtimePermission && getSystemService(NotificationManager::class.java).areNotificationsEnabled()
    }

    private fun registerNetworkCallback() {
        if (networkCallbackRegistered) return
        try {
            defaultNetwork = connectivityManager.activeNetwork
            defaultNetworkHasInternet = defaultNetwork?.let(::networkHasInternet) ?: false
            connectivityManager.registerDefaultNetworkCallback(networkCallback)
            networkCallbackRegistered = true
        } catch (_: Exception) {
            setState(StatusStore.STATE_ERROR, "无法监听网络状态")
        }
    }

    private fun hasInternetNetwork(): Boolean {
        val network = connectivityManager.activeNetwork ?: return false
        if (network == defaultNetwork) return defaultNetworkHasInternet
        return networkHasInternet(network)
    }

    private fun networkHasInternet(network: Network): Boolean {
        val capabilities = connectivityManager.getNetworkCapabilities(network) ?: return false
        return capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
    }

    private fun connectNow(reason: String) {
        if (destroyed || stopRequested) return
        if (!hasInternetNetwork()) {
            setState(StatusStore.STATE_RECONNECTING, "等待网络恢复")
            return
        }
        val session = secretStore.session()
        if (!session.isPrivate) {
            setState(StatusStore.STATE_AUTH_FAILED, "请重新登录")
            return
        }
        synchronized(socketLock) {
            if (webSocket != null) return
            socketGeneration += 1
            val generation = socketGeneration
            setState(StatusStore.STATE_CONNECTING, if (reason == "service start") "正在连接" else "正在重新连接")
            val request = Request.Builder()
                .url(AppConfig.websocketUrl(session.ntfyWebsocketUrl, cursorStore.read()))
                .header("Authorization", "Bearer ${session.ntfyToken}")
                .header("User-Agent", "AgentWatch-Android/${BuildConfig.VERSION_NAME}")
                .build()
            webSocket = client.newWebSocket(request, Listener(generation))
        }
    }

    private inner class Listener(private val generation: Long) : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            if (!isCurrent(generation, webSocket)) {
                webSocket.close(1000, "superseded")
                return
            }
            reconnectAttempt = 0
            setState(StatusStore.STATE_CONNECTED, "实时连接已建立")
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            if (!isCurrent(generation, webSocket)) return
            handleMessage(webSocket, text)
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            onMessage(webSocket, bytes.utf8())
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(code, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            if (!clearIfCurrent(generation, webSocket)) return
            if (!destroyed && !stopRequested) scheduleReconnect("连接已关闭")
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (!clearIfCurrent(generation, webSocket)) return
            if (response?.code == 401 || response?.code == 403) {
                setState(StatusStore.STATE_AUTH_FAILED, "登录凭据已失效，请重新登录")
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            } else if (!destroyed && !stopRequested) {
                Log.w(LOG_TAG, "WebSocket connection failed (${t.javaClass.simpleName})")
                scheduleReconnect("连接中断，正在自动重连")
            }
        }
    }

    private fun handleMessage(socket: WebSocket, text: String) {
        val message = NtfyMessage.parse(text) ?: return
        when (message.event) {
            "open" -> {
                cursorStore.establishBaseline(message.time)
                setState(StatusStore.STATE_CONNECTED, "实时连接已建立")
            }
            "keepalive" -> Unit
            "message" -> {
                val session = secretStore.session()
                if (!session.isPrivate || message.topic != session.ntfyTopic || message.id.isBlank() || message.eventKey.isBlank()) return
                val now = System.currentTimeMillis() / 1000L
                if (!message.isForDevice(DeviceIdentity.notificationTarget(this))) {
                    cursorStore.advance(message.time)
                    return
                }
                if (message.isExpired(now) || message.isTooOld(now)) {
                    cursorStore.advance(message.time)
                    return
                }
                try {
                    val historyResult = historyStore.insert(session.username, message)
                    historyStore.cleanup(session.username, historySettings.retentionDays())
                    if (historyResult == HistoryStore.InsertResult.INSERTED) {
                        sendBroadcast(Intent(AppConfig.HISTORY_ACTION).setPackage(packageName))
                    }
                    if (shouldSuppressFromHistory(historyResult)) {
                        cursorStore.advance(message.time)
                        return
                    }
                    val notificationTimeMillis = when {
                        message.sentAt > 0L -> message.sentAt * 1000L
                        message.time > 0L -> message.time * 1000L
                        else -> System.currentTimeMillis()
                    }
                    when (
                        val claim = dedupeStore.claim(
                            message.eventKey,
                            message.source,
                            notificationTimeMillis,
                        )
                    ) {
                        EventDedupeStore.Claim.SHOWN -> {
                            if (shouldRemoveUnexpectedInsert(EventDedupeStore.Claim.SHOWN, historyResult)) {
                                historyStore.deleteOne(session.username, message.eventKey, remember = false)
                            }
                            ackOutbox.enqueue(message.eventKey)
                            flushAcknowledgements()
                        }
                        EventDedupeStore.Claim.SUPPRESSED -> Unit
                        EventDedupeStore.Claim.DISPLAY_COMMITTED -> {
                            if (shouldRemoveUnexpectedInsert(EventDedupeStore.Claim.DISPLAY_COMMITTED, historyResult)) {
                                historyStore.deleteOne(session.username, message.eventKey, remember = false)
                            }
                            queueAcknowledgementForCommittedDisplay(message.eventKey)
                            statusStore.markReceived()
                            flushAcknowledgements()
                        }
                        EventDedupeStore.Claim.NEW,
                        EventDedupeStore.Claim.PENDING_DISPLAY,
                        -> {
                            val silentRecovery = claim == EventDedupeStore.Claim.PENDING_DISPLAY
                            if (silentRecovery && renderer.isEventActive(message.eventKey)) {
                                commitDisplayedDelivery(message.eventKey)
                                statusStore.markReceived()
                                flushAcknowledgements()
                                cursorStore.advance(message.time)
                                return
                            }
                            val blockReason = renderer.eventBlockReason(message.source, silentRecovery)
                            if (blockReason != null) {
                                dedupeStore.markSuppressed(message.eventKey)
                                setState(StatusStore.STATE_PERMISSION_REQUIRED, blockReason)
                            } else {
                                renderer.showEvent(message, silent = silentRecovery)
                                commitDisplayedDelivery(message.eventKey)
                                statusStore.markReceived()
                                flushAcknowledgements()
                            }
                        }
                    }
                    cursorStore.advance(message.time)
                } catch (exception: Exception) {
                    Log.e(LOG_TAG, "Could not post notification", exception)
                    setState(StatusStore.STATE_ERROR, "系统未能显示通知，正在重试")
                    socket.cancel()
                }
            }
        }
    }

    /**
     * Reconciles the only two non-terminal display stages before the WebSocket
     * can deliver new messages. PENDING_DISPLAY is ambiguous by design: the
     * process may have died immediately before or after NotificationManager's
     * binder call. An active notification proves the call committed; otherwise
     * a metadata-only notification is posted on a silent channel first.
     */
    private fun recoverIncompleteDeliveries(): Boolean = try {
        val account = secretStore.session().username
        dedupeStore.incompleteDeliveries().forEach { delivery ->
            val active = renderer.isEventActive(delivery.eventKey)
            when (EventDedupeStore.recoveryAction(delivery.stage, active)) {
                EventDedupeStore.RecoveryAction.POST_SILENT_THEN_QUEUE_ACK -> {
                    val blockReason = renderer.eventBlockReason(delivery.source, silentRecovery = true)
                    if (blockReason != null) {
                        dedupeStore.markSuppressed(delivery.eventKey)
                        setState(StatusStore.STATE_PERMISSION_REQUIRED, blockReason)
                    } else {
                        renderer.showRecovery(
                            delivery.eventKey,
                            delivery.source,
                            delivery.notificationTimeMillis,
                            historyStore.find(account, delivery.eventKey),
                        )
                        commitDisplayedDelivery(delivery.eventKey)
                        statusStore.markReceived()
                    }
                }
                EventDedupeStore.RecoveryAction.COMMIT_ACTIVE_THEN_QUEUE_ACK -> {
                    commitDisplayedDelivery(delivery.eventKey)
                    statusStore.markReceived()
                }
                EventDedupeStore.RecoveryAction.QUEUE_ACK -> {
                    queueAcknowledgementForCommittedDisplay(delivery.eventKey)
                    statusStore.markReceived()
                }
                EventDedupeStore.RecoveryAction.NONE -> Unit
            }
        }
        true
    } catch (exception: Exception) {
        Log.e(LOG_TAG, "Could not recover notification delivery state", exception)
        setState(StatusStore.STATE_ERROR, "无法恢复通知送达状态，请重新打开应用")
        false
    }

    private fun commitDisplayedDelivery(eventKey: String) {
        dedupeStore.markDisplayCommitted(eventKey)
        queueAcknowledgementForCommittedDisplay(eventKey)
    }

    private fun queueAcknowledgementForCommittedDisplay(eventKey: String) {
        ackOutbox.enqueue(eventKey)
        dedupeStore.markShown(eventKey)
    }

    @Synchronized
    private fun flushAcknowledgements() {
        if (destroyed || stopRequested || ackInFlight) return
        val appToken = secretStore.get(SecretStore.APP_TOKEN)
        if (appToken.isBlank()) return
        val pending = ackOutbox.nextDue(System.currentTimeMillis())
        if (pending == null) {
            scheduleNextAcknowledgement()
            return
        }
        ackRunnable?.let(handler::removeCallbacks)
        ackRunnable = null
        ackInFlight = true
        registrationClient.acknowledge(appToken, pending.eventId) { result ->
            handler.post {
                ackInFlight = false
                if (destroyed || stopRequested) return@post
                when (result) {
                    RegistrationClient.AcknowledgeResult.ACKNOWLEDGED -> {
                        ackOutbox.markAcknowledged(pending.eventId)
                        statusStore.markAcknowledged()
                        flushAcknowledgements()
                    }
                    RegistrationClient.AcknowledgeResult.AUTH_REJECTED -> {
                        setState(StatusStore.STATE_AUTH_FAILED, "登录凭据已失效，请重新登录")
                        stopForeground(STOP_FOREGROUND_REMOVE)
                        stopSelf()
                    }
                    RegistrationClient.AcknowledgeResult.RETRYABLE_FAILURE -> {
                        ackOutbox.markFailed(pending.eventId, System.currentTimeMillis())
                        flushAcknowledgements()
                    }
                }
            }
        }
    }

    private fun scheduleNextAcknowledgement() {
        val next = ackOutbox.nextAttemptAtMillis() ?: return
        val delay = maxOf(next - System.currentTimeMillis(), 1_000L)
        ackRunnable?.let(handler::removeCallbacks)
        ackRunnable = Runnable {
            ackRunnable = null
            flushAcknowledgements()
        }.also { handler.postDelayed(it, delay) }
    }

    private fun scheduleReconnect(detail: String, minimumDelaySeconds: Int = 0) {
        if (destroyed || stopRequested) return
        reconnectRunnable?.let(handler::removeCallbacks)
        val delaySeconds = maxOf(minimumDelaySeconds, backoffSeconds(reconnectAttempt))
        reconnectAttempt = min(reconnectAttempt + 1, 20)
        val delayMillis = delaySeconds * 1000L + Random.nextLong(0L, 1_001L)
        setState(StatusStore.STATE_RECONNECTING, "$detail（约 ${delaySeconds} 秒）")
        reconnectRunnable = Runnable {
            reconnectRunnable = null
            connectNow("retry")
        }.also { handler.postDelayed(it, delayMillis) }
    }

    private fun closeSocket(reason: String) {
        val socket = synchronized(socketLock) {
            socketGeneration += 1
            webSocket.also { webSocket = null }
        }
        socket?.close(1001, reason.take(100))
    }

    private fun isCurrent(generation: Long, socket: WebSocket): Boolean =
        synchronized(socketLock) { generation == socketGeneration && webSocket === socket }

    private fun clearIfCurrent(generation: Long, socket: WebSocket): Boolean =
        synchronized(socketLock) {
            if (generation != socketGeneration || webSocket !== socket) return@synchronized false
            webSocket = null
            true
        }

    private fun setState(state: String, detail: String) {
        statusStore.update(state, detail)
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(
            NotificationRenderer.FOREGROUND_NOTIFICATION_ID,
            renderer.connectionNotification(state, detail),
        )
    }

    private fun promoteToForeground(state: String, detail: String) {
        val notification = renderer.connectionNotification(state, detail)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(
                NotificationRenderer.FOREGROUND_NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_REMOTE_MESSAGING,
            )
        } else {
            startForeground(NotificationRenderer.FOREGROUND_NOTIFICATION_ID, notification)
        }
    }

    companion object {
        private const val LOG_TAG = "AgentWatch"

        internal enum class NetworkCapabilityAction { NONE, CONNECT, DISCONNECT }

        internal fun networkCapabilityAction(
            isCurrentDefault: Boolean,
            hadInternet: Boolean,
            hasInternet: Boolean,
        ): NetworkCapabilityAction = when {
            !isCurrentDefault || hadInternet == hasInternet -> NetworkCapabilityAction.NONE
            hasInternet -> NetworkCapabilityAction.CONNECT
            else -> NetworkCapabilityAction.DISCONNECT
        }

        internal fun backoffSeconds(attempt: Int): Int {
            if (attempt <= 0) return 1
            return min(1 shl min(attempt, 6), 60)
        }

        internal fun shouldSuppressFromHistory(result: HistoryStore.InsertResult): Boolean =
            result == HistoryStore.InsertResult.DELETED

        /**
         * If delivery metadata proves an event was already handled, a newly
         * inserted row can only be a replay after the user or retention policy
         * removed that history. Do not resurrect it. SUPPRESSED is excluded:
         * an event first received while its notification channel is blocked
         * must remain visible in the in-app archive.
         */
        internal fun shouldRemoveUnexpectedInsert(
            claim: EventDedupeStore.Claim,
            result: HistoryStore.InsertResult,
        ): Boolean = result == HistoryStore.InsertResult.INSERTED &&
            claim in setOf(EventDedupeStore.Claim.SHOWN, EventDedupeStore.Claim.DISPLAY_COMMITTED)
    }
}
