package io.github.taotaoxu7447.agentwatch

import android.content.Context

class HistorySettings(context: Context) {
    private val preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    fun retentionDays(): Int = normalizeRetentionDays(preferences.getInt(KEY_RETENTION_DAYS, DEFAULT_DAYS))

    fun setRetentionDays(days: Int) {
        require(days in ALLOWED_DAYS)
        preferences.edit().putInt(KEY_RETENTION_DAYS, days).apply()
    }

    companion object {
        const val DEFAULT_DAYS = 7
        const val PERMANENT = 0
        const val MAX_MESSAGES_PER_ACCOUNT = 500
        val ALLOWED_DAYS = setOf(1, 7, 30, PERMANENT)

        internal fun normalizeRetentionDays(value: Int): Int =
            if (value in ALLOWED_DAYS) value else DEFAULT_DAYS

        internal fun cutoffMillis(nowMillis: Long, retentionDays: Int): Long? =
            retentionDays.takeIf { it > 0 }?.let { nowMillis - it * 86_400_000L }

        private const val PREFERENCES = "agentwatch_history_settings"
        private const val KEY_RETENTION_DAYS = "retention_days"
    }
}
