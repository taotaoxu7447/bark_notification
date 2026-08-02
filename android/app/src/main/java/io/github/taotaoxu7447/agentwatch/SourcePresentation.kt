package io.github.taotaoxu7447.agentwatch

/**
 * Keeps source-specific Android presentation in one place so notification
 * channels, status-bar icons, and history rows cannot drift apart.
 */
internal object SourcePresentation {
    fun channelId(source: NtfyMessage.Source): String = "event_${source.key}_v1"

    fun recoveryChannelId(source: NtfyMessage.Source): String =
        "event_${source.key}_recovery_v1"

    fun smallIcon(source: NtfyMessage.Source): Int = when (source) {
        NtfyMessage.Source.CODEX -> R.drawable.ic_notify_codex
        NtfyMessage.Source.ZCODE -> R.drawable.ic_notify_zcode
        NtfyMessage.Source.KIMI -> R.drawable.ic_notify_kimi
        NtfyMessage.Source.GROK -> R.drawable.ic_notify_grok
        NtfyMessage.Source.CLAUDE -> R.drawable.ic_notify_claude
        NtfyMessage.Source.OTHER -> R.drawable.ic_notify_other
    }

    fun largeIcon(source: NtfyMessage.Source): Int? = when (source) {
        NtfyMessage.Source.CODEX -> R.drawable.source_codex
        NtfyMessage.Source.ZCODE -> R.drawable.source_zcode
        NtfyMessage.Source.KIMI -> R.drawable.source_kimi
        NtfyMessage.Source.GROK -> R.drawable.source_grok
        NtfyMessage.Source.CLAUDE -> R.drawable.source_claude
        NtfyMessage.Source.OTHER -> null
    }
}
