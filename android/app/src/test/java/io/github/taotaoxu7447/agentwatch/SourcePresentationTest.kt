package io.github.taotaoxu7447.agentwatch

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

class SourcePresentationTest {
    @Test
    fun claudeUsesDedicatedChannelsAndIcons() {
        val claude = NtfyMessage.Source.CLAUDE
        assertEquals("event_claude_v1", SourcePresentation.channelId(claude))
        assertEquals("event_claude_recovery_v1", SourcePresentation.recoveryChannelId(claude))
        assertEquals(R.drawable.ic_notify_claude, SourcePresentation.smallIcon(claude))
        assertEquals(R.drawable.source_claude, SourcePresentation.largeIcon(claude))
    }

    @Test
    fun claudePresentationDoesNotReuseAnotherSource() {
        val claude = NtfyMessage.Source.CLAUDE
        NtfyMessage.Source.entries.filter { it != claude }.forEach { other ->
            assertNotEquals(SourcePresentation.channelId(other), SourcePresentation.channelId(claude))
            assertNotEquals(SourcePresentation.smallIcon(other), SourcePresentation.smallIcon(claude))
            assertNotEquals(SourcePresentation.largeIcon(other), SourcePresentation.largeIcon(claude))
        }
    }
}
