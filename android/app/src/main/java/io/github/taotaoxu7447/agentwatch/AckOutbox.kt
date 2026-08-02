package io.github.taotaoxu7447.agentwatch

import android.annotation.SuppressLint
import android.content.Context
import org.json.JSONObject
import kotlin.math.min

@SuppressLint("ApplySharedPref")
class AckOutbox(context: Context) {
    data class Pending(val eventId: String, val nextAttemptAtMillis: Long)

    private val preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    @Synchronized
    fun enqueue(eventId: String) {
        require(eventId.isNotBlank())
        val entries = load()
        if (entries.has(eventId)) return
        entries.put(
            eventId,
            JSONObject()
                .put("state", "pending")
                .put("attempts", 0)
                .put("next", 0L)
                .put("at", System.currentTimeMillis()),
        )
        trim(entries)
        save(entries)
    }

    @Synchronized
    fun nextDue(nowMillis: Long): Pending? = load().let { entries ->
        entries.keys().asSequence()
            .mapNotNull { eventId ->
                val value = entries.optJSONObject(eventId) ?: return@mapNotNull null
                if (value.optString("state") != "pending") return@mapNotNull null
                Pending(eventId, value.optLong("next", 0L))
            }
            .filter { it.nextAttemptAtMillis <= nowMillis }
            .minByOrNull { it.nextAttemptAtMillis }
    }

    @Synchronized
    fun nextAttemptAtMillis(): Long? = load().let { entries ->
        entries.keys().asSequence()
            .mapNotNull { key -> entries.optJSONObject(key) }
            .filter { it.optString("state") == "pending" }
            .map { it.optLong("next", 0L) }
            .minOrNull()
    }

    @Synchronized
    fun markAcknowledged(eventId: String) {
        val entries = load()
        entries.put(
            eventId,
            JSONObject().put("state", "acknowledged").put("at", System.currentTimeMillis()),
        )
        trim(entries)
        save(entries)
    }

    @Synchronized
    fun markFailed(eventId: String, nowMillis: Long): Long {
        val entries = load()
        val current = entries.optJSONObject(eventId) ?: JSONObject()
        val attempts = min(current.optInt("attempts", 0) + 1, 30)
        val next = nowMillis + retryDelaySeconds(attempts) * 1000L
        entries.put(
            eventId,
            current
                .put("state", "pending")
                .put("attempts", attempts)
                .put("next", next)
                .put("at", nowMillis),
        )
        trim(entries)
        save(entries)
        return next
    }

    @Synchronized
    fun clear() {
        check(preferences.edit().clear().commit()) { "Could not clear acknowledgement outbox" }
    }

    private fun load(): JSONObject = try {
        JSONObject(preferences.getString(KEY_ENTRIES, "{}") ?: "{}")
    } catch (_: Exception) {
        JSONObject()
    }

    private fun save(entries: JSONObject) {
        // ACK state must survive a process death after display commit.
        check(preferences.edit().putString(KEY_ENTRIES, entries.toString()).commit()) {
            "Could not persist acknowledgement outbox"
        }
    }

    private fun trim(entries: JSONObject) {
        if (entries.length() <= MAX_ENTRIES) return
        val removable = entries.keys().asSequence()
            .mapNotNull { key ->
                val value = entries.optJSONObject(key) ?: return@mapNotNull null
                if (value.optString("state") != "acknowledged") return@mapNotNull null
                key to value.optLong("at", 0L)
            }
            .sortedBy { it.second }
            .take(entries.length() - MAX_ENTRIES)
            .map { it.first }
            .toList()
        removable.forEach(entries::remove)
    }

    companion object {
        private const val PREFERENCES = "agentwatch_ack_outbox"
        private const val KEY_ENTRIES = "entries"
        private const val MAX_ENTRIES = 512

        internal fun retryDelaySeconds(attempt: Int): Long {
            val exponent = min(maxOf(attempt - 1, 0), 6)
            return min(5L * (1L shl exponent), 300L)
        }
    }
}
