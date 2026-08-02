package io.github.taotaoxu7447.agentwatch

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Space
import android.widget.TextView
import android.widget.Toast
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : Activity() {
    private lateinit var secretStore: SecretStore
    private lateinit var statusStore: StatusStore
    private lateinit var registrationClient: RegistrationClient
    private lateinit var logoutStateStore: LogoutStateStore

    private lateinit var authPanel: LinearLayout
    private lateinit var sessionPanel: LinearLayout
    private lateinit var usernameInput: EditText
    private lateinit var passwordInput: EditText
    private lateinit var inviteInput: EditText
    private lateinit var deviceNameInput: EditText
    private lateinit var registerButton: Button
    private lateinit var loginButton: Button
    private lateinit var accountText: TextView
    private lateinit var statusText: TextView
    private lateinit var statusDetailText: TextView
    private lateinit var lastDeliveryText: TextView
    private lateinit var testButton: Button
    private lateinit var logoutButton: Button
    private var receiverRegistered = false
    private var startAfterPermission = false
    private var logoutRequestInFlight = false

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) = refreshStatus()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        secretStore = SecretStore(this)
        statusStore = StatusStore(this)
        registrationClient = RegistrationClient(this)
        logoutStateStore = LogoutStateStore(this)
        NotificationRenderer(this).createChannels()
        setContentView(buildContent())
        refreshSession()
    }

    @SuppressLint("UnspecifiedRegisterReceiverFlag")
    override fun onStart() {
        super.onStart()
        if (!receiverRegistered) {
            val filter = IntentFilter(AppConfig.STATUS_ACTION)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                registerReceiver(statusReceiver, filter, RECEIVER_NOT_EXPORTED)
            } else {
                @Suppress("DEPRECATION")
                // The exported-state overload was introduced in API 33. This
                // legacy branch only runs on older releases.
                registerReceiver(statusReceiver, filter)
            }
            receiverRegistered = true
        }
    }

    override fun onResume() {
        super.onResume()
        refreshSession()
        if (logoutStateStore.isPending()) {
            resumePendingLogout(userInitiated = false)
            return
        }
        if (
            isConfigured() &&
            statusStore.snapshot().state != StatusStore.STATE_AUTH_FAILED &&
            notificationsAllowed()
        ) {
            startReceiverService()
        }
    }

    override fun onStop() {
        if (receiverRegistered) {
            unregisterReceiver(statusReceiver)
            receiverRegistered = false
        }
        super.onStop()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_NOTIFICATIONS) return
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED && startAfterPermission) {
            startAfterPermission = false
            startReceiverService()
        } else {
            statusStore.update(StatusStore.STATE_PERMISSION_REQUIRED, "没有通知权限，任务完成时无法提醒")
            toast("需要允许通知权限才能接收任务提醒")
        }
        refreshStatus()
    }

    private fun buildContent(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(22), dp(24), dp(22), dp(36))
            setBackgroundColor(Color.rgb(245, 247, 251))
        }

        root.addView(text("AgentWatch", 31f, bold = true).apply { setTextColor(Color.rgb(20, 33, 61)) })
        root.addView(text("任务完成，手机和手表立即提醒", 16f).apply {
            setTextColor(Color.rgb(77, 91, 124))
            setPadding(0, dp(4), 0, dp(22))
        })

        val statusCard = card().apply {
            addView(text("实时连接", 14f, bold = true).apply { setTextColor(Color.rgb(77, 91, 124)) })
            statusText = text("未启动", 23f, bold = true).apply {
                setTextColor(Color.rgb(20, 33, 61))
                setPadding(0, dp(6), 0, 0)
            }
            addView(statusText)
            statusDetailText = text("登录后会自动连接", 14f).apply {
                setTextColor(Color.rgb(77, 91, 124))
                setPadding(0, dp(5), 0, 0)
            }
            addView(statusDetailText)
            lastDeliveryText = text("尚未收到送达回执", 13f).apply {
                setTextColor(Color.rgb(103, 116, 143))
                setPadding(0, dp(12), 0, 0)
            }
            addView(lastDeliveryText)
        }
        root.addView(statusCard)
        root.addView(space(16))

        authPanel = card().apply {
            addView(sectionTitle("注册一次，后续自动连接"))
            addView(helpText("服务器地址已经内置。新用户输入邀请代码、账号和密码即可；已有账号直接登录。"))
            usernameInput = input("账号（3–32 位，首尾为字母或数字）")
            addView(usernameInput)
            passwordInput = input("密码（至少 12 位）").apply {
                inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            }
            addView(passwordInput)
            inviteInput = input("新用户邀请代码")
            addView(inviteInput)
            deviceNameInput = input("设备名称").apply { setText(DeviceIdentity.defaultName()) }
            addView(deviceNameInput)
            registerButton = primaryButton("注册并连接") { authenticate(register = true) }
            addView(registerButton)
            loginButton = secondaryButton("已有账号登录") { authenticate(register = false) }
            addView(loginButton)
        }
        root.addView(authPanel)

        sessionPanel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE

            addView(card().apply {
                addView(sectionTitle("当前设备"))
                accountText = text("", 16f, bold = true).apply { setTextColor(Color.rgb(20, 33, 61)) }
                addView(accountText)
                addView(helpText("${BuildConfig.SERVER_BASE_URL} · ${BuildConfig.NTFY_TOPIC}\n凭据仅加密保存在本机，不会显示在界面或日志中。"))
                addView(primaryButton("重新连接") {
                    startReceiverService(forceReconnect = true)
                    toast("正在重新连接")
                })
                testButton = secondaryButton("发送一条端到端测试") { sendEndToEndTest() }
                addView(testButton)
                logoutButton = secondaryButton("退出并撤销此设备") { logout() }
                addView(logoutButton)
            })
            addView(space(16))
            addView(card().apply {
                addView(sectionTitle("后台送达设置"))
                addView(helpText("首次使用请允许通知、自启动，并把电池管理设为“完全允许后台行为”。这些系统选项必须由你亲自确认。"))
                addView(primaryButton("打开通知设置") { BackgroundSettings.openNotificationSettings(this@MainActivity) })
                addView(secondaryButton("打开自启动设置") { BackgroundSettings.openAutoStartSettings(this@MainActivity) })
                addView(secondaryButton("打开电池设置") { BackgroundSettings.openBatterySettings(this@MainActivity) })
            })
            addView(space(16))
            addView(card().apply {
                addView(sectionTitle("提醒范围"))
                addView(helpText("只显示 Codex、ZCode、Kimi Code、Grok Build 的主任务完成、异常或需要处理提醒。没有消息列表，也不会回放首次安装前的缓存。"))
                addView(helpText("版本 ${BuildConfig.VERSION_NAME} · WebSocket only"))
            })
        }
        root.addView(sessionPanel)

        return ScrollView(this).apply {
            isFillViewport = true
            addView(root, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        }
    }

    private fun authenticate(register: Boolean) {
        val username = usernameInput.text.toString().trim()
        val password = passwordInput.text.toString()
        val inviteCode = inviteInput.text.toString().trim()
        val deviceName = deviceNameInput.text.toString().trim().ifBlank { DeviceIdentity.defaultName() }
        if (!username.matches(Regex("[A-Za-z0-9](?:[A-Za-z0-9_.-]{1,30}[A-Za-z0-9])"))) {
            toast("账号格式不正确")
            return
        }
        if (password.length < 12) {
            toast("密码至少需要 12 位")
            return
        }
        if (register && inviteCode.isBlank()) {
            toast("新用户需要输入邀请代码")
            return
        }
        setAuthButtonsEnabled(false)
        val callback: (Result<RegistrationClient.AuthSession>) -> Unit = { result ->
            runOnUiThread {
                setAuthButtonsEnabled(true)
                result.onSuccess(::saveSession).onFailure { error -> toast(error.message ?: "登录失败") }
            }
        }
        if (register) {
            registrationClient.register(username, password, inviteCode, deviceName, callback)
        } else {
            registrationClient.login(username, password, deviceName, callback)
        }
    }

    private fun saveSession(session: RegistrationClient.AuthSession) {
        val changedAccount = DeviceIdentity.username(this) != session.username ||
            secretStore.get(SecretStore.NTFY_TOKEN) != session.ntfyToken
        if (changedAccount) {
            CursorStore(this).reset()
            EventDedupeStore(this).clear()
            AckOutbox(this).clear()
        }
        secretStore.put(SecretStore.NTFY_TOKEN, session.ntfyToken)
        secretStore.put(SecretStore.APP_TOKEN, session.appToken)
        DeviceIdentity.setUsername(this, session.username)
        logoutStateStore.clear()
        passwordInput.text.clear()
        inviteInput.text.clear()
        refreshSession()
        toast("登录成功，正在建立实时连接")
        ensurePermissionAndStart()
    }

    private fun ensurePermissionAndStart() {
        if (notificationsAllowed()) {
            startReceiverService()
            return
        }
        if (runtimeNotificationPermissionGranted()) {
            statusStore.update(StatusStore.STATE_PERMISSION_REQUIRED, "系统通知总开关已关闭")
            toast("请先点击“打开通知设置”并开启 AgentWatch 通知")
            refreshStatus()
            return
        }
        startAfterPermission = true
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQUEST_NOTIFICATIONS)
        }
    }

    private fun startReceiverService(forceReconnect: Boolean = false) {
        if (!isConfigured()) return
        if (!notificationsAllowed()) {
            ensurePermissionAndStart()
            return
        }
        val intent = Intent(this, WatchService::class.java)
        if (forceReconnect) intent.action = AppConfig.ACTION_RECONNECT
        try {
            startForegroundService(intent)
        } catch (_: Exception) {
            statusStore.update(StatusStore.STATE_ERROR, "系统阻止了后台服务，请检查自启动和电池设置")
        }
    }

    private fun sendEndToEndTest() {
        if (statusStore.snapshot().state != StatusStore.STATE_CONNECTED) {
            toast("请等待首页显示“已连接”后再测试")
            return
        }
        val token = secretStore.get(SecretStore.APP_TOKEN)
        if (token.isBlank()) {
            toast("请重新登录")
            return
        }
        testButton.isEnabled = false
        registrationClient.sendTest(token) { result ->
            runOnUiThread {
                testButton.isEnabled = true
                result.onSuccess {
                    toast("测试已发出，等待 WebSocket 通知")
                }.onFailure { error -> toast(error.message ?: "测试发送失败") }
            }
        }
    }

    private fun logout() {
        val appToken = secretStore.get(SecretStore.APP_TOKEN)
        if (appToken.isBlank()) {
            clearLocalSession()
            return
        }
        try {
            logoutStateStore.markPending()
        } catch (_: IllegalStateException) {
            toast("无法保存退出状态，请释放存储空间后重试")
            return
        }
        stopService(Intent(this, WatchService::class.java))
        statusStore.update(StatusStore.STATE_STOPPED, "正在撤销此设备的服务器凭据")
        resumePendingLogout(userInitiated = true)
    }

    private fun resumePendingLogout(userInitiated: Boolean) {
        if (logoutRequestInFlight) return
        val appToken = secretStore.get(SecretStore.APP_TOKEN)
        if (appToken.isBlank()) {
            clearLocalSession()
            if (userInitiated) toast("已退出登录")
            return
        }
        logoutRequestInFlight = true
        logoutButton.isEnabled = false
        registrationClient.logout(appToken) { result ->
            runOnUiThread {
                logoutRequestInFlight = false
                logoutButton.isEnabled = true
                result.onSuccess {
                    clearLocalSession()
                    toast("此设备的服务器凭据已撤销")
                }.onFailure { error ->
                    statusStore.update(StatusStore.STATE_ERROR, "服务器尚未确认退出；再次打开应用会自动重试")
                    toast(error.message ?: "服务器尚未确认退出，请稍后重试")
                }
            }
        }
    }

    private fun clearLocalSession() {
        stopService(Intent(this, WatchService::class.java))
        secretStore.clearSession()
        DeviceIdentity.clearUsername(this)
        CursorStore(this).reset()
        EventDedupeStore(this).clear()
        AckOutbox(this).clear()
        statusStore.update(StatusStore.STATE_STOPPED, "已退出登录")
        logoutStateStore.clear()
        refreshSession()
    }

    private fun refreshSession() {
        val configured = isConfigured()
        val authenticationFailed = statusStore.snapshot().state == StatusStore.STATE_AUTH_FAILED
        authPanel.visibility = if (configured && !authenticationFailed) View.GONE else View.VISIBLE
        sessionPanel.visibility = if (configured) View.VISIBLE else View.GONE
        if (configured) {
            accountText.text = getString(
                R.string.account_and_device,
                DeviceIdentity.username(this),
                DeviceIdentity.defaultName(),
            )
        }
        refreshStatus()
    }

    private fun refreshStatus() {
        val snapshot = statusStore.snapshot()
        statusText.text = when (snapshot.state) {
            StatusStore.STATE_CONNECTED -> "已连接"
            StatusStore.STATE_CONNECTING -> "连接中"
            StatusStore.STATE_RECONNECTING -> "自动重连中"
            StatusStore.STATE_PERMISSION_REQUIRED -> "需要通知权限"
            StatusStore.STATE_AUTH_FAILED -> "需要重新登录"
            StatusStore.STATE_ERROR -> "需要检查设置"
            else -> "未启动"
        }
        statusText.setTextColor(
            when (snapshot.state) {
                StatusStore.STATE_CONNECTED -> Color.rgb(20, 145, 83)
                StatusStore.STATE_AUTH_FAILED, StatusStore.STATE_ERROR -> Color.rgb(196, 55, 67)
                else -> Color.rgb(20, 33, 61)
            },
        )
        statusDetailText.text = snapshot.detail.ifBlank {
            if (isConfigured()) "等待接收服务状态" else "登录后会自动连接"
        }
        lastDeliveryText.text = when {
            snapshot.lastAcknowledgedAt > 0L -> "服务器已收到送达回执：${formatTime(snapshot.lastAcknowledgedAt)}"
            snapshot.lastReceivedAt > 0L -> "最近已显示通知：${formatTime(snapshot.lastReceivedAt)}"
            else -> "尚未收到送达回执"
        }
    }

    private fun isConfigured(): Boolean =
        secretStore.get(SecretStore.NTFY_TOKEN).isNotBlank() && DeviceIdentity.username(this).isNotBlank()

    private fun runtimeNotificationPermissionGranted(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    private fun notificationsAllowed(): Boolean =
        runtimeNotificationPermissionGranted() &&
            getSystemService(NotificationManager::class.java).areNotificationsEnabled()

    private fun setAuthButtonsEnabled(enabled: Boolean) {
        registerButton.isEnabled = enabled
        loginButton.isEnabled = enabled
    }

    private fun card(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(18), dp(18), dp(18), dp(18))
        background = GradientDrawable().apply {
            setColor(Color.WHITE)
            cornerRadius = dp(18).toFloat()
            setStroke(dp(1), Color.rgb(226, 231, 241))
        }
        elevation = dp(1).toFloat()
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
    }

    private fun sectionTitle(value: String): TextView = text(value, 19f, bold = true).apply {
        setTextColor(Color.rgb(20, 33, 61))
        setPadding(0, 0, 0, dp(8))
    }

    private fun helpText(value: String): TextView = text(value, 14f).apply {
        setTextColor(Color.rgb(77, 91, 124))
        setLineSpacing(0f, 1.15f)
        setPadding(0, 0, 0, dp(12))
    }

    private fun input(hintValue: String): EditText = EditText(this).apply {
        hint = hintValue
        textSize = 16f
        setSingleLine(true)
        setPadding(dp(14), dp(12), dp(14), dp(12))
        background = GradientDrawable().apply {
            setColor(Color.rgb(249, 250, 253))
            cornerRadius = dp(12).toFloat()
            setStroke(dp(1), Color.rgb(210, 217, 231))
        }
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52)).apply {
            bottomMargin = dp(10)
        }
    }

    private fun primaryButton(label: String, action: () -> Unit): Button = button(label, true, action)
    private fun secondaryButton(label: String, action: () -> Unit): Button = button(label, false, action)

    private fun button(label: String, primary: Boolean, action: () -> Unit): Button = Button(this).apply {
        text = label
        textSize = 15f
        isAllCaps = false
        gravity = Gravity.CENTER
        setTypeface(typeface, Typeface.BOLD)
        setTextColor(if (primary) Color.WHITE else Color.rgb(49, 92, 245))
        background = GradientDrawable().apply {
            setColor(if (primary) Color.rgb(49, 92, 245) else Color.rgb(237, 241, 255))
            cornerRadius = dp(12).toFloat()
        }
        setOnClickListener { action() }
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(50)).apply {
            topMargin = dp(6)
        }
    }

    private fun text(value: String, size: Float, bold: Boolean = false): TextView = TextView(this).apply {
        text = value
        textSize = size
        if (bold) setTypeface(typeface, Typeface.BOLD)
    }

    private fun space(height: Int): Space = Space(this).apply {
        layoutParams = LinearLayout.LayoutParams(1, dp(height))
    }

    private fun formatTime(epochMillis: Long): String =
        SimpleDateFormat("MM-dd HH:mm:ss", Locale.getDefault()).format(Date(epochMillis))

    private fun toast(message: String) = Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    private fun dp(value: Int): Int = (value * resources.displayMetrics.density + 0.5f).toInt()

    companion object {
        private const val REQUEST_NOTIFICATIONS = 2001
    }
}
