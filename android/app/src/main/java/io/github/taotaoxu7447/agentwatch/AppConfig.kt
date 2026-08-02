package io.github.taotaoxu7447.agentwatch

import okhttp3.HttpUrl.Companion.toHttpUrl

object AppConfig {
    const val TOPIC: String = BuildConfig.NTFY_TOPIC
    const val STATUS_ACTION: String = "io.github.taotaoxu7447.agentwatch.STATUS"
    const val ACTION_RECONNECT: String = "io.github.taotaoxu7447.agentwatch.RECONNECT"
    const val ACTION_STOP: String = "io.github.taotaoxu7447.agentwatch.STOP"

    fun websocketUrl(cursor: CursorStore.Cursor): String {
        val builder = BuildConfig.SERVER_BASE_URL.toHttpUrl().newBuilder()
            .addPathSegment(TOPIC)
            .addPathSegment("ws")
        if (cursor.serverEpochSeconds > 0L) {
            // ntfy's timestamp cursor uses >=. Replaying the final second is
            // intentional: persistent event-key dedupe prevents alerts while
            // ensuring two messages from the same second cannot be missed.
            builder.addQueryParameter("since", cursor.serverEpochSeconds.toString())
        }
        return builder.build().toString().replaceFirst("https://", "wss://")
    }

    fun apiUrl(path: String): String =
        BuildConfig.SERVER_BASE_URL.toHttpUrl().newBuilder()
            .addEncodedPathSegments(BuildConfig.API_PREFIX.trim('/'))
            .addPathSegment(path.trim('/'))
            .build()
            .toString()
}
