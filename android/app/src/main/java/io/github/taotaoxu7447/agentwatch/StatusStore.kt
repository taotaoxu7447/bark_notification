package io.github.taotaoxu7447.agentwatch

import android.content.Context
import android.content.Intent

class StatusStore(private val context: Context) {
    private val preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    data class Snapshot(
        val state: String,
        val detail: String,
        val changedAt: Long,
        val lastReceivedAt: Long,
        val lastAcknowledgedAt: Long,
    )

    fun update(state: String, detail: String = "") {
        preferences.edit()
            .putString(KEY_STATE, state)
            .putString(KEY_DETAIL, detail.take(160))
            .putLong(KEY_CHANGED_AT, System.currentTimeMillis())
            .apply()
        context.sendBroadcast(Intent(AppConfig.STATUS_ACTION).setPackage(context.packageName))
    }

    fun markReceived() {
        preferences.edit().putLong(KEY_LAST_RECEIVED, System.currentTimeMillis()).apply()
        context.sendBroadcast(Intent(AppConfig.STATUS_ACTION).setPackage(context.packageName))
    }

    fun markAcknowledged() {
        preferences.edit().putLong(KEY_LAST_ACK, System.currentTimeMillis()).apply()
        context.sendBroadcast(Intent(AppConfig.STATUS_ACTION).setPackage(context.packageName))
    }

    fun snapshot(): Snapshot = Snapshot(
        state = preferences.getString(KEY_STATE, STATE_STOPPED) ?: STATE_STOPPED,
        detail = preferences.getString(KEY_DETAIL, "") ?: "",
        changedAt = preferences.getLong(KEY_CHANGED_AT, 0L),
        lastReceivedAt = preferences.getLong(KEY_LAST_RECEIVED, 0L),
        lastAcknowledgedAt = preferences.getLong(KEY_LAST_ACK, 0L),
    )

    companion object {
        const val STATE_STOPPED = "stopped"
        const val STATE_CONNECTING = "connecting"
        const val STATE_CONNECTED = "connected"
        const val STATE_RECONNECTING = "reconnecting"
        const val STATE_PERMISSION_REQUIRED = "permission_required"
        const val STATE_AUTH_FAILED = "auth_failed"
        const val STATE_ERROR = "error"

        private const val PREFERENCES = "agentwatch_status"
        private const val KEY_STATE = "state"
        private const val KEY_DETAIL = "detail"
        private const val KEY_CHANGED_AT = "changed_at"
        private const val KEY_LAST_RECEIVED = "last_received"
        private const val KEY_LAST_ACK = "last_ack"
    }
}
