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
            val title = json.optString("title")
            NtfyMessage(
                id = json.optString("id"),
                sequenceId = json.optString("sequence_id"),
                event = json.optString("event"),
                topic = json.optString("topic"),
                time = json.optLong("time"),
                expires = json.optLong("expires"),
                title = title,
                message = json.optString("message"),
                priority = json.optInt("priority", 3),
                tags = tags,
                source = inferSource(tags, title),
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
                else -> Source.OTHER
            }
        }
    }
}
