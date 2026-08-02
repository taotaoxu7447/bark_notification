package io.github.taotaoxu7447.agentwatch

import org.json.JSONObject

data class NtfyMessage(
    val id: String,
    val sequenceId: String,
    val event: String,
    val topic: String,
    val time: Long,
    val expires: Long,
    val title: String,
    val message: String,
    val priority: Int,
    val tags: Set<String>,
    val source: Source,
    val computerId: String = "",
    val computerName: String = "",
    val sentAt: Long = 0L,
) {
    val eventKey: String get() = sequenceId.ifBlank { id }

    fun isForDevice(target: String): Boolean {
        val targetTags = tags.filter { it.startsWith("target_") }
        return targetTags.isEmpty() || "target_$target" in targetTags
    }

    fun isExpired(nowEpochSeconds: Long): Boolean = expires > 0L && expires <= nowEpochSeconds

    fun isTooOld(nowEpochSeconds: Long): Boolean =
        time > 0L && nowEpochSeconds - time > BuildConfig.MAX_CATCH_UP_SECONDS

    enum class Source(val key: String, val displayName: String) {
        CODEX("codex", "Codex"),
        ZCODE("zcode", "ZCode"),
        KIMI("kimi", "Kimi Code"),
        GROK("grok", "Grok Build"),
        CLAUDE("claude", "Claude Code"),
        OTHER("other", "其他任务"),
    }

    companion object {
        fun parse(text: String): NtfyMessage? = try {
            val json = JSONObject(text)
            val tagsJson = json.optJSONArray("tags")
            val tags = buildSet {
                if (tagsJson != null) {
                    for (index in 0 until tagsJson.length()) {
                        tagsJson.optString(index).takeIf { it.isNotBlank() }?.let(::add)
                    }
                }
            }
            val wireTitle = json.optString("title")
            val wireBody = json.optString("message")
            val wireSequenceId = json.optString("sequence_id")
            val envelope = if ("agentwatch_v2" in tags) {
                if (wireSequenceId.isBlank()) return null
                val candidate = try {
                    JSONObject(wireBody)
                } catch (_: Exception) {
                    return null
                }
                if (
                    candidate.optString("schema") != "agentwatch_event_v2" ||
                    candidate.optString("event_id").isBlank() ||
                    candidate.optString("event_id") != wireSequenceId
                ) {
                    return null
                }
                candidate
            } else {
                null
            }
            val title = envelope?.optString("title")?.ifBlank { wireTitle } ?: wireTitle
            val eventId = envelope?.optString("event_id").orEmpty()
            val sourceKey = envelope?.optString("source")?.lowercase().orEmpty()
            val source = if (sourceKey in Source.entries.map { it.key }) sourceForKey(sourceKey) else inferSource(tags, title)
            NtfyMessage(
                id = json.optString("id"),
                sequenceId = eventId.ifBlank { wireSequenceId },
                event = json.optString("event"),
                topic = json.optString("topic"),
                time = json.optLong("time"),
                expires = json.optLong("expires"),
                title = title,
                message = envelope?.optString("body") ?: wireBody,
                priority = json.optInt("priority", 3),
                tags = tags,
                source = source,
                computerId = envelope?.optString("computer_id").orEmpty(),
                computerName = envelope?.optString("computer_name").orEmpty(),
                sentAt = envelope?.optLong("sent_at") ?: 0L,
            )
        } catch (_: Exception) {
            null
        }

        internal fun inferSource(tags: Set<String>, title: String): Source {
            Source.entries.firstOrNull { source -> "source_${source.key}" in tags }
                ?.let { return it }
            val normalized = title.lowercase()
            return when {
                normalized.startsWith("codex") -> Source.CODEX
                normalized.startsWith("zcode") -> Source.ZCODE
                normalized.startsWith("kimi") -> Source.KIMI
                normalized.startsWith("grok") -> Source.GROK
                normalized.startsWith("claude") -> Source.CLAUDE
                else -> Source.OTHER
            }
        }

        internal fun sourceForKey(key: String): Source =
            Source.entries.firstOrNull { it.key == key.lowercase() } ?: Source.OTHER
    }
}
