package io.github.taotaoxu7447.agentwatch

import android.content.Context
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

class RegistrationClient(private val context: Context) {
    data class AuthSession(val username: String, val ntfyToken: String, val appToken: String)

    enum class AcknowledgeResult {
        ACKNOWLEDGED,
        AUTH_REJECTED,
        RETRYABLE_FAILURE,
    }

    internal class HttpStatusException(
        val statusCode: Int,
        message: String,
    ) : IllegalStateException(message)

    private val client = OkHttpClient.Builder()
        .connectTimeout(12, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .callTimeout(20, TimeUnit.SECONDS)
        .build()

    fun register(
        username: String,
        password: String,
        inviteCode: String,
        deviceName: String,
        callback: (Result<AuthSession>) -> Unit,
    ) {
        val payload = baseAuthPayload(username, password, deviceName).put("invite_code", inviteCode)
        post("register", payload, "", callback = authCallback(callback))
    }

    fun login(
        username: String,
        password: String,
        deviceName: String,
        callback: (Result<AuthSession>) -> Unit,
    ) {
        post("login", baseAuthPayload(username, password, deviceName), "", callback = authCallback(callback))
    }

    fun sendTest(appToken: String, callback: (Result<String>) -> Unit) {
        val payload = JSONObject().put("source", "codex")
        post("test", payload, appToken) { result ->
            callback(result.map { it.optString("event_id") })
        }
    }

    fun acknowledge(appToken: String, eventId: String, callback: (AcknowledgeResult) -> Unit) {
        if (appToken.isBlank() || eventId.isBlank()) {
            callback(AcknowledgeResult.AUTH_REJECTED)
            return
        }
        val payload = JSONObject().put("event_id", eventId)
        post("ack", payload, appToken) { result -> callback(classifyAcknowledgement(result.exceptionOrNull())) }
    }

    fun logout(appToken: String, callback: (Result<Unit>) -> Unit) {
        post("logout", JSONObject(), appToken) { result ->
            callback(normalizeLogoutResult(result.exceptionOrNull()))
        }
    }

    private fun baseAuthPayload(username: String, password: String, deviceName: String): JSONObject =
        JSONObject()
            .put("username", username.trim())
            .put("password", password)
            .put("device_id", DeviceIdentity.id(context))
            .put("device_name", deviceName.trim().take(80))

    private fun authCallback(callback: (Result<AuthSession>) -> Unit): (Result<JSONObject>) -> Unit = { result ->
        callback(
            result.mapCatching { json ->
                val session = AuthSession(
                    username = json.getString("username"),
                    ntfyToken = json.getString("ntfy_token"),
                    appToken = json.getString("app_token"),
                )
                require(session.ntfyToken.startsWith("tk_")) { "服务器返回了无效订阅凭据" }
                require(session.appToken.length >= 24) { "服务器返回了无效登录凭据" }
                session
            },
        )
    }

    private fun post(
        path: String,
        payload: JSONObject,
        appToken: String,
        callback: (Result<JSONObject>) -> Unit,
    ) {
        val requestBuilder = Request.Builder()
            .url(AppConfig.apiUrl(path))
            .header("User-Agent", "AgentWatch-Android/${BuildConfig.VERSION_NAME}")
            .header("Accept", "application/json")
            .post(payload.toString().toRequestBody(JSON_MEDIA_TYPE))
        if (appToken.isNotBlank()) requestBuilder.header("Authorization", "Bearer $appToken")
        client.newCall(requestBuilder.build()).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                callback(Result.failure(IOException("无法连接注册服务器，请检查网络", e)))
            }

            override fun onResponse(call: Call, response: Response) {
                response.use {
                    val text = it.body.string().take(16_384)
                    val json = try {
                        JSONObject(text.ifBlank { "{}" })
                    } catch (_: Exception) {
                        JSONObject()
                    }
                    if (!it.isSuccessful) {
                        val message = json.optString("error").ifBlank { "服务器请求失败 (${it.code})" }
                        callback(Result.failure(HttpStatusException(it.code, message)))
                    } else {
                        callback(Result.success(json))
                    }
                }
            }
        })
    }

    companion object {
        private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()

        internal fun classifyAcknowledgement(error: Throwable?): AcknowledgeResult = when {
            error == null -> AcknowledgeResult.ACKNOWLEDGED
            remoteSessionIsAbsent(error) -> AcknowledgeResult.AUTH_REJECTED
            else -> AcknowledgeResult.RETRYABLE_FAILURE
        }

        internal fun remoteSessionIsAbsent(error: Throwable?): Boolean =
            error is HttpStatusException && error.statusCode in setOf(401, 403)

        internal fun normalizeLogoutResult(error: Throwable?): Result<Unit> = when {
            error == null || remoteSessionIsAbsent(error) -> Result.success(Unit)
            else -> Result.failure(error)
        }
    }
}
