package io.github.taotaoxu7447.agentwatch

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action !in setOf(Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED)) return
        if (LogoutStateStore(context).isPending()) return
        if (!SecretStore(context).session().isPrivate) return
        try {
            context.startForegroundService(Intent(context, WatchService::class.java))
        } catch (_: Exception) {
            StatusStore(context).update(StatusStore.STATE_ERROR, "系统阻止了开机自启动，请打开 AgentWatch")
        }
    }
}
