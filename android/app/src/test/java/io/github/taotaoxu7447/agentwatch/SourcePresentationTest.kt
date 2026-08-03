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

    @Test
    fun piAndOpenCodeUseDedicatedChannelsAndIcons() {
        val expected = listOf(
            NtfyMessage.Source.PI to Triple(
                "event_pi_v1",
                R.drawable.ic_notify_pi,
                R.drawable.source_pi,
            ),
            NtfyMessage.Source.OPENCODE to Triple(
                "event_opencode_v1",
                R.drawable.ic_notify_opencode,
                R.drawable.source_opencode,
            ),
        )

        expected.forEach { (source, presentation) ->
            assertEquals(presentation.first, SourcePresentation.channelId(source))
            assertEquals(
                presentation.first.removeSuffix("_v1") + "_recovery_v1",
                SourcePresentation.recoveryChannelId(source),
            )
            assertEquals(presentation.second, SourcePresentation.smallIcon(source))
            assertEquals(presentation.third, SourcePresentation.largeIcon(source))
        }
    }

    @Test
    fun piAndOpenCodePresentationsDoNotReuseAnyOtherSource() {
        listOf(NtfyMessage.Source.PI, NtfyMessage.Source.OPENCODE).forEach { source ->
            NtfyMessage.Source.entries.filter { it != source }.forEach { other ->
                assertNotEquals(SourcePresentation.channelId(other), SourcePresentation.channelId(source))
                assertNotEquals(SourcePresentation.smallIcon(other), SourcePresentation.smallIcon(source))
                assertNotEquals(SourcePresentation.largeIcon(other), SourcePresentation.largeIcon(source))
            }
        }
    }
}
