package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class NtfyMessageTest {
    @Test
    fun sourceTagWinsOverTitleFallback() {
        assertEquals(
            NtfyMessage.Source.KIMI,
            NtfyMessage.inferSource(
                setOf("agentwatch_v1", "source_agentwatch_test", "source_kimi"),
                "Codex 已完成",
            ),
        )
    }

    @Test
    fun oldMessagesCanFallBackToTitle() {
        assertEquals(NtfyMessage.Source.GROK, NtfyMessage.inferSource(emptySet(), "Grok Build 已完成"))
        assertEquals(NtfyMessage.Source.OTHER, NtfyMessage.inferSource(emptySet(), "任务已完成"))
    }

    @Test
    fun stableSequenceIdIsTheDedupeKey() {
        val message = NtfyMessage(
            id = "ntfy-id",
            sequenceId = "aw1_mac_task",
            event = "message",
            topic = "agent-watch",
            time = 1_785_600_123L,
            expires = 0L,
            title = "Codex 已完成",
            message = "完成",
            priority = 3,
            tags = setOf("agentwatch_v1", "source_codex"),
            source = NtfyMessage.Source.CODEX,
        )
        assertEquals("aw1_mac_task", message.eventKey)
        assertEquals(NtfyMessage.Source.CODEX, message.source)
        assertFalse(message.isTooOld(1_785_600_123L + BuildConfig.MAX_CATCH_UP_SECONDS))
        assertTrue(message.isTooOld(1_785_600_124L + BuildConfig.MAX_CATCH_UP_SECONDS))
        assertTrue(message.isForDevice("device-a"))
        assertTrue(message.copy(tags = message.tags + "target_device-a").isForDevice("device-a"))
        assertFalse(message.copy(tags = message.tags + "target_device-b").isForDevice("device-a"))
    }
}
