package io.github.taotaoxu7447.agentwatch

import okhttp3.HttpUrl.Companion.toHttpUrl

object AppConfig {
    const val STATUS_ACTION: String = "io.github.taotaoxu7447.agentwatch.STATUS"
    const val HISTORY_ACTION: String = "io.github.taotaoxu7447.agentwatch.HISTORY"
    const val ACTION_RECONNECT: String = "io.github.taotaoxu7447.agentwatch.RECONNECT"
    const val ACTION_STOP: String = "io.github.taotaoxu7447.agentwatch.STOP"

    fun websocketUrl(ntfyWebsocketUrl: String, cursor: CursorStore.Cursor): String {
        require(ntfyWebsocketUrl.startsWith("wss://"))
        val builder = ntfyWebsocketUrl.replaceFirst("wss://", "https://").toHttpUrl().newBuilder()
        if (cursor.serverEpochSeconds > 0L) {
            // ntfy's timestamp cursor uses >=. Replaying the final second is
            // intentional: persistent event-key dedupe prevents alerts while
            // ensuring two messages from the same second cannot be missed.
            builder.addQueryParameter("since", cursor.serverEpochSeconds.toString())
        }
        return builder.build().toString().replaceFirst("https://", "wss://")
    }

    fun validPrivateSession(topic: String, ntfyUrl: String, ntfyWebsocketUrl: String): Boolean = try {
        if (!ntfyWebsocketUrl.startsWith("wss://")) return false
        val configuredServer = BuildConfig.SERVER_BASE_URL.toHttpUrl()
        val publish = ntfyUrl.toHttpUrl()
        val websocket = ntfyWebsocketUrl.replaceFirst("wss://", "https://").toHttpUrl()
        topic.matches(Regex("aw-[0-9a-f]{32}")) &&
            publish.scheme == "https" &&
            publish.username.isBlank() && publish.password.isBlank() &&
            publish.query == null && publish.fragment == null &&
            publish.host == configuredServer.host &&
            publish.port == configuredServer.port &&
            websocket.username.isBlank() && websocket.password.isBlank() &&
            websocket.query == null && websocket.fragment == null &&
            websocket.host == configuredServer.host &&
            websocket.port == configuredServer.port &&
            publish.encodedPathSegments == listOf(topic) &&
            websocket.encodedPathSegments == listOf(topic, "ws")
    } catch (_: IllegalArgumentException) {
        false
    }

    fun apiUrl(path: String): String =
        BuildConfig.SERVER_BASE_URL.toHttpUrl().newBuilder()
            .addEncodedPathSegments(BuildConfig.API_PREFIX.trim('/'))
            .addEncodedPathSegments(path.trim('/'))
            .build()
            .toString()
}
