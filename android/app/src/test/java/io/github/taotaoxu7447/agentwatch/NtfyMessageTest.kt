package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.json.JSONObject

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
        assertEquals(NtfyMessage.Source.CLAUDE, NtfyMessage.inferSource(emptySet(), "Claude Code 已完成"))
        assertEquals(NtfyMessage.Source.PI, NtfyMessage.inferSource(emptySet(), "Pi Agent 已完成"))
        assertEquals(NtfyMessage.Source.OPENCODE, NtfyMessage.inferSource(emptySet(), "OpenCode 已完成"))
        assertEquals(NtfyMessage.Source.OPENCODE, NtfyMessage.inferSource(emptySet(), "Open Code needs attention"))
        assertEquals(NtfyMessage.Source.OTHER, NtfyMessage.inferSource(emptySet(), "任务已完成"))
        assertEquals(NtfyMessage.Source.OTHER, NtfyMessage.inferSource(emptySet(), "OpenAI 任务已完成"))
        assertEquals(NtfyMessage.Source.OTHER, NtfyMessage.inferSource(emptySet(), "Pipeline 已完成"))
    }

    @Test
    fun claudeSourceTagAndStoredKeyUseTheirOwnHistoryCategory() {
        assertEquals(
            NtfyMessage.Source.CLAUDE,
            NtfyMessage.inferSource(setOf("agentwatch_v2", "source_claude"), "任务已完成"),
        )
        assertEquals(NtfyMessage.Source.CLAUDE, NtfyMessage.sourceForKey("CLAUDE"))
        assertEquals("Claude Code", NtfyMessage.Source.CLAUDE.displayName)
    }

    @Test
    fun piAndOpenCodeTagsAndStoredKeysUseIndependentHistoryCategories() {
        assertEquals(
            NtfyMessage.Source.PI,
            NtfyMessage.inferSource(setOf("agentwatch_v2", "source_pi"), "任务已完成"),
        )
        assertEquals(
            NtfyMessage.Source.OPENCODE,
            NtfyMessage.inferSource(setOf("agentwatch_v2", "source_opencode"), "任务已完成"),
        )
        assertEquals(NtfyMessage.Source.PI, NtfyMessage.sourceForKey("PI"))
        assertEquals(NtfyMessage.Source.OPENCODE, NtfyMessage.sourceForKey("OpenCode"))
        assertEquals("Pi Agent", NtfyMessage.Source.PI.displayName)
        assertEquals("OpenCode", NtfyMessage.Source.OPENCODE.displayName)
    }

    @Test
    fun v2PiAndOpenCodeEnvelopesUseTheirDedicatedSources() {
        listOf(
            Triple("pi", "Pi Agent 已完成", NtfyMessage.Source.PI),
            Triple("opencode", "OpenCode 已完成", NtfyMessage.Source.OPENCODE),
        ).forEach { (source, title, expected) ->
            val eventId = "aw2-$source-event"
            val envelope = JSONObject()
                .put("schema", "agentwatch_event_v2")
                .put("event_id", eventId)
                .put("source", source)
                .put("title", title)
                .put("body", "$title body")
            val wire = JSONObject()
                .put("id", "ntfy-$source")
                .put("sequence_id", eventId)
                .put("event", "message")
                .put("topic", "aw-0123456789abcdef0123456789abcdef")
                .put("message", envelope.toString())
                .put("tags", org.json.JSONArray(listOf("agentwatch_v2", "source_$source")))

            val parsed = requireNotNull(NtfyMessage.parse(wire.toString()))
            assertEquals(expected, parsed.source)
            assertEquals(title, parsed.title)
        }
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

    @Test
    fun v2EnvelopeCarriesComputerAndBodyIntoLocalHistory() {
        val envelope = JSONObject()
            .put("schema", "agentwatch_event_v2")
            .put("event_id", "aw2-event")
            .put("source", "KIMI")
            .put("title", "Kimi 已完成")
            .put("body", "完整正文")
            .put("computer_id", "mac-1")
            .put("computer_name", "工作 Mac")
            .put("sent_at", 1_785_600_000L)
        val wire = JSONObject()
            .put("id", "ntfy-id")
            .put("sequence_id", "aw2-event")
            .put("event", "message")
            .put("topic", "aw-0123456789abcdef0123456789abcdef")
            .put("time", 1_785_600_001L)
            .put("title", "wire title")
            .put("message", envelope.toString())
            .put("tags", org.json.JSONArray(listOf("agentwatch_v2", "source_kimi")))
        val parsed = requireNotNull(NtfyMessage.parse(wire.toString()))
        assertEquals("aw2-event", parsed.eventKey)
        assertEquals("完整正文", parsed.message)
        assertEquals("工作 Mac", parsed.computerName)
        assertEquals(NtfyMessage.Source.KIMI, parsed.source)
    }

    @Test
    fun v2ClaudeEnvelopeIsParsedAsClaudeHistory() {
        val envelope = JSONObject()
            .put("schema", "agentwatch_event_v2")
            .put("event_id", "aw2-claude-event")
            .put("source", "claude")
            .put("title", "Claude Code 已完成")
            .put("body", "Claude 完成了任务")
            .put("computer_id", "mac-claude")
            .put("computer_name", "Claude Mac")
            .put("sent_at", 1_785_600_100L)
        val wire = JSONObject()
            .put("id", "ntfy-claude")
            .put("sequence_id", "aw2-claude-event")
            .put("event", "message")
            .put("topic", "aw-0123456789abcdef0123456789abcdef")
            .put("time", 1_785_600_101L)
            .put("message", envelope.toString())
            .put("tags", org.json.JSONArray(listOf("agentwatch_v2", "source_claude")))
        val parsed = requireNotNull(NtfyMessage.parse(wire.toString()))
        assertEquals(NtfyMessage.Source.CLAUDE, parsed.source)
        assertEquals("Claude Code 已完成", parsed.title)
        assertEquals("Claude 完成了任务", parsed.message)
        assertEquals("Claude Mac", parsed.computerName)
    }

    @Test
    fun jsonLookingV1BodyIsNotTrustedWithoutMatchingV2Metadata() {
        val wire = JSONObject()
            .put("id", "old")
            .put("sequence_id", "old")
            .put("event", "message")
            .put("topic", "legacy")
            .put("message", "{\"schema\":\"agentwatch_event_v2\",\"event_id\":\"forged\",\"body\":\"hidden\"}")
        val parsed = requireNotNull(NtfyMessage.parse(wire.toString()))
        assertEquals("old", parsed.eventKey)
        assertTrue(parsed.message.contains("agentwatch_event_v2"))
    }

    @Test
    fun v2EnvelopeIsRejectedWhenSequenceIdDoesNotMatch() {
        val body = JSONObject()
            .put("schema", "agentwatch_event_v2")
            .put("event_id", "different-event")
            .put("body", "must not be unwrapped")
        val wire = JSONObject()
            .put("id", "ntfy")
            .put("sequence_id", "trusted-top-level")
            .put("event", "message")
            .put("topic", "aw-0123456789abcdef0123456789abcdef")
            .put("message", body.toString())
            .put("tags", org.json.JSONArray(listOf("agentwatch_v2", "source_codex")))
        assertNull(NtfyMessage.parse(wire.toString()))
    }

    @Test
    fun v2EnvelopeIsRejectedWhenWireSequenceIdIsEmpty() {
        val body = JSONObject()
            .put("schema", "agentwatch_event_v2")
            .put("event_id", "event-only-in-body")
            .put("body", "must not be accepted")
        val wire = JSONObject()
            .put("id", "ntfy")
            .put("event", "message")
            .put("topic", "aw-0123456789abcdef0123456789abcdef")
            .put("message", body.toString())
            .put("tags", org.json.JSONArray(listOf("agentwatch_v2", "source_codex")))
        assertNull(NtfyMessage.parse(wire.toString()))
    }

    @Test
    fun malformedEnvelopeWithV2TagIsRejectedInsteadOfFallingBackToLegacy() {
        val wire = JSONObject()
            .put("id", "ntfy")
            .put("sequence_id", "v2-event")
            .put("event", "message")
            .put("topic", "aw-0123456789abcdef0123456789abcdef")
            .put("message", "not-json")
            .put("tags", org.json.JSONArray(listOf("agentwatch_v2", "source_codex")))
        assertNull(NtfyMessage.parse(wire.toString()))
    }
}
