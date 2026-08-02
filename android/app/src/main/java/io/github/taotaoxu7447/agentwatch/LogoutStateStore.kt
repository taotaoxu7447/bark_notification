package io.github.taotaoxu7447.agentwatch

import android.annotation.SuppressLint
import android.content.Context

/**
 * A durable intent marker for logout's unavoidable network/process boundary.
 *
 * It is written before the revoke request and cleared only after local secrets
 * have been removed. A later Activity can therefore finish a logout whose
 * successful HTTP response was lost or whose process was killed.
 */
@SuppressLint("ApplySharedPref")
class LogoutStateStore(context: Context) {
    private val preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    fun isPending(): Boolean = preferences.getBoolean(KEY_PENDING, false)

    fun deleteHistory(): Boolean = preferences.getBoolean(KEY_DELETE_HISTORY, false)

    fun markPending(deleteHistory: Boolean) {
        check(
            preferences.edit()
                .putBoolean(KEY_PENDING, true)
                .putBoolean(KEY_DELETE_HISTORY, deleteHistory)
                .commit(),
        ) {
            "Could not persist pending logout"
        }
    }

    fun clear() {
        check(preferences.edit().clear().commit()) {
            "Could not clear pending logout"
        }
    }

    companion object {
        private const val PREFERENCES = "agentwatch_logout_state"
        private const val KEY_PENDING = "pending"
        private const val KEY_DELETE_HISTORY = "delete_history"
    }
}
