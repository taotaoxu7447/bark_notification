package io.github.taotaoxu7447.agentwatch

import android.annotation.SuppressLint
import android.content.Context

@SuppressLint("ApplySharedPref")
class CursorStore(context: Context) {
    private val preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    data class Cursor(val serverEpochSeconds: Long)

    @Synchronized
    fun read(): Cursor = Cursor(serverEpochSeconds = preferences.getLong(KEY_SERVER_TIME, 0L))

    @Synchronized
    fun establishBaseline(serverEpochSeconds: Long) {
        if (serverEpochSeconds <= 0L || preferences.getLong(KEY_SERVER_TIME, 0L) > 0L) return
        // Cursor writes must be durable before the next WebSocket message is accepted.
        check(preferences.edit().putLong(KEY_SERVER_TIME, serverEpochSeconds).commit()) {
            "Could not persist notification cursor"
        }
    }

    @Synchronized
    fun advance(serverEpochSeconds: Long) {
        if (serverEpochSeconds <= 0L) return
        check(
            preferences.edit()
                .putLong(KEY_SERVER_TIME, maxOf(serverEpochSeconds, preferences.getLong(KEY_SERVER_TIME, 0L)))
                .commit(),
        ) { "Could not advance notification cursor" }
    }

    @Synchronized
    fun reset() {
        check(preferences.edit().clear().commit()) { "Could not reset notification cursor" }
    }

    companion object {
        private const val PREFERENCES = "agentwatch_cursor"
        private const val KEY_SERVER_TIME = "server_epoch_seconds"
    }
}
