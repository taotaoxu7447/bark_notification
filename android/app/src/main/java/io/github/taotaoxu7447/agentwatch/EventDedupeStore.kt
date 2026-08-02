package io.github.taotaoxu7447.agentwatch

import android.annotation.SuppressLint
import android.content.Context
import org.json.JSONObject

/**
 * Durable notification-display state. This store contains only routing
 * metadata; the separate app-private HistoryStore owns the user-visible body.
 */
@SuppressLint("ApplySharedPref")
class EventDedupeStore(context: Context) {
    private val preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    enum class Stage(val storedValue: String) {
        PENDING_DISPLAY("pending_display"),
        DISPLAY_COMMITTED("display_committed"),
        SHOWN("shown"),
        SUPPRESSED("suppressed"),
    }

    enum class Claim { NEW, PENDING_DISPLAY, DISPLAY_COMMITTED, SHOWN, SUPPRESSED }

    enum class RecoveryAction {
        POST_SILENT_THEN_QUEUE_ACK,
        COMMIT_ACTIVE_THEN_QUEUE_ACK,
        QUEUE_ACK,
        NONE,
    }

    data class IncompleteDelivery(
        val eventKey: String,
        val source: NtfyMessage.Source,
        val notificationTimeMillis: Long,
        val stage: Stage,
    )

    @Synchronized
    fun claim(
        eventKey: String,
        source: NtfyMessage.Source,
        notificationTimeMillis: Long,
    ): Claim {
        require(eventKey.isNotBlank())
        val entries = load()
        val existing = entries.optJSONObject(eventKey)
        if (existing != null) {
            val stage = parseStage(existing.optString(KEY_STATE))
            if (stage == Stage.PENDING_DISPLAY && existing.optString(KEY_SOURCE).isBlank()) {
                existing.put(KEY_SOURCE, source.key)
                existing.put(KEY_NOTIFICATION_TIME, notificationTimeMillis)
                save(entries)
            }
            return claimForStage(stage)
        }
        entries.put(
            eventKey,
            entry(Stage.PENDING_DISPLAY, source, notificationTimeMillis),
        )
        trimTerminalEntries(entries)
        save(entries)
        return Claim.NEW
    }

    @Synchronized
    fun markDisplayCommitted(eventKey: String) {
        updateStage(eventKey, Stage.DISPLAY_COMMITTED)
    }

    @Synchronized
    fun markShown(eventKey: String) {
        updateStage(eventKey, Stage.SHOWN)
    }

    @Synchronized
    fun markSuppressed(eventKey: String) {
        updateStage(eventKey, Stage.SUPPRESSED)
    }

    @Synchronized
    fun incompleteDeliveries(): List<IncompleteDelivery> {
        val entries = load()
        return entries.keys().asSequence()
            .mapNotNull { eventKey ->
                val value = entries.optJSONObject(eventKey) ?: return@mapNotNull null
                val stage = parseStage(value.optString(KEY_STATE))
                if (stage !in setOf(Stage.PENDING_DISPLAY, Stage.DISPLAY_COMMITTED)) {
                    return@mapNotNull null
                }
                IncompleteDelivery(
                    eventKey = eventKey,
                    source = sourceForKey(value.optString(KEY_SOURCE)),
                    notificationTimeMillis = value.optLong(KEY_NOTIFICATION_TIME, 0L),
                    stage = stage,
                )
            }
            .sortedBy { it.notificationTimeMillis }
            .toList()
    }

    @Synchronized
    fun clear() {
        check(preferences.edit().clear().commit()) { "Could not clear notification state" }
    }

    private fun updateStage(eventKey: String, stage: Stage) {
        val entries = load()
        val value = entries.optJSONObject(eventKey) ?: throw IllegalStateException("Missing notification state")
        value.put(KEY_STATE, stage.storedValue).put(KEY_UPDATED_AT, System.currentTimeMillis())
        trimTerminalEntries(entries)
        save(entries)
    }

    private fun entry(
        stage: Stage,
        source: NtfyMessage.Source,
        notificationTimeMillis: Long,
    ): JSONObject = JSONObject()
        .put(KEY_STATE, stage.storedValue)
        .put(KEY_SOURCE, source.key)
        .put(KEY_NOTIFICATION_TIME, notificationTimeMillis)
        .put(KEY_UPDATED_AT, System.currentTimeMillis())

    private fun load(): JSONObject = try {
        JSONObject(preferences.getString(KEY_ENTRIES, "{}") ?: "{}")
    } catch (_: Exception) {
        JSONObject()
    }

    private fun save(entries: JSONObject) {
        check(preferences.edit().putString(KEY_ENTRIES, entries.toString()).commit()) {
            "Could not persist notification state"
        }
    }

    private fun trimTerminalEntries(entries: JSONObject) {
        if (entries.length() <= MAX_TERMINAL_ENTRIES) return
        val removeCount = entries.length() - MAX_TERMINAL_ENTRIES
        val removable = entries.keys().asSequence()
            .mapNotNull { key ->
                val value = entries.optJSONObject(key) ?: return@mapNotNull null
                val stage = parseStage(value.optString(KEY_STATE))
                if (stage in setOf(Stage.PENDING_DISPLAY, Stage.DISPLAY_COMMITTED)) return@mapNotNull null
                key to value.optLong(KEY_UPDATED_AT, 0L)
            }
            .sortedBy { it.second }
            .take(removeCount)
            .map { it.first }
            .toList()
        removable.forEach(entries::remove)
    }

    companion object {
        private const val PREFERENCES = "agentwatch_dedupe"
        private const val KEY_ENTRIES = "entries"
        private const val KEY_STATE = "state"
        private const val KEY_SOURCE = "source"
        private const val KEY_NOTIFICATION_TIME = "notification_time"
        private const val KEY_UPDATED_AT = "at"
        private const val MAX_TERMINAL_ENTRIES = 512

        internal fun recoveryAction(stage: Stage, notificationActive: Boolean): RecoveryAction = when (stage) {
            Stage.PENDING_DISPLAY -> if (notificationActive) {
                RecoveryAction.COMMIT_ACTIVE_THEN_QUEUE_ACK
            } else {
                RecoveryAction.POST_SILENT_THEN_QUEUE_ACK
            }
            Stage.DISPLAY_COMMITTED -> RecoveryAction.QUEUE_ACK
            Stage.SHOWN, Stage.SUPPRESSED -> RecoveryAction.NONE
        }

        private fun parseStage(value: String): Stage = when (value) {
            Stage.DISPLAY_COMMITTED.storedValue -> Stage.DISPLAY_COMMITTED
            Stage.SHOWN.storedValue -> Stage.SHOWN
            Stage.SUPPRESSED.storedValue -> Stage.SUPPRESSED
            // "pending" is accepted for pre-state-machine development builds.
            else -> Stage.PENDING_DISPLAY
        }

        private fun claimForStage(stage: Stage): Claim = when (stage) {
            Stage.PENDING_DISPLAY -> Claim.PENDING_DISPLAY
            Stage.DISPLAY_COMMITTED -> Claim.DISPLAY_COMMITTED
            Stage.SHOWN -> Claim.SHOWN
            Stage.SUPPRESSED -> Claim.SUPPRESSED
        }

        private fun sourceForKey(key: String): NtfyMessage.Source =
            NtfyMessage.Source.entries.firstOrNull { it.key == key } ?: NtfyMessage.Source.OTHER
    }
}
