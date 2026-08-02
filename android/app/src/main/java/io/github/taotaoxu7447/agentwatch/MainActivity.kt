package io.github.taotaoxu7447.agentwatch

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
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
import android.text.Editable
import android.text.InputType
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.HorizontalScrollView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.Space
import android.widget.TextView
import android.widget.Toast
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : Activity() {
    private enum class Page { MESSAGES, DEVICES, SETTINGS }

    private lateinit var secretStore: SecretStore
    private lateinit var statusStore: StatusStore
    private lateinit var registrationClient: RegistrationClient
    private lateinit var logoutStateStore: LogoutStateStore
    private lateinit var historyStore: HistoryStore
    private lateinit var historySettings: HistorySettings

    private lateinit var authPanel: LinearLayout
    private lateinit var navigation: LinearLayout
    private lateinit var pageContainer: LinearLayout
    private lateinit var messagesPage: LinearLayout
    private lateinit var devicesPage: LinearLayout
    private lateinit var settingsPage: LinearLayout
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
    private lateinit var searchInput: EditText
    private lateinit var categoryRow: LinearLayout
    private lateinit var messageList: LinearLayout
    private lateinit var computerList: LinearLayout
    private lateinit var historySizeText: TextView
    private val navButtons = mutableMapOf<Page, Button>()
    private var selectedPage = Page.MESSAGES
    private var selectedSource: NtfyMessage.Source? = null
    private var receiverRegistered = false
    private var startAfterPermission = false
    private var logoutRequestInFlight = false
    private var upgradeInFlight = false
    private var computersInFlight = false
    private var pendingEventId = ""

    private val appReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                AppConfig.HISTORY_ACTION -> refreshMessages()
                else -> refreshStatus()
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        secretStore = SecretStore(this)
        statusStore = StatusStore(this)
        registrationClient = RegistrationClient(this)
        logoutStateStore = LogoutStateStore(this)
        historyStore = HistoryStore(this)
        historySettings = HistorySettings(this)
        historyStore.cleanupAll(historySettings.retentionDays())
        NotificationRenderer(this).createChannels()
        pendingEventId = intent.getStringExtra(EXTRA_EVENT_ID).orEmpty()
        setContentView(buildContent())
        refreshSession()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        pendingEventId = intent.getStringExtra(EXTRA_EVENT_ID).orEmpty()
        if (pendingEventId.isNotBlank()) showPage(Page.MESSAGES)
        showPendingDetail()
    }

    @SuppressLint("UnspecifiedRegisterReceiverFlag")
    override fun onStart() {
        super.onStart()
        if (!receiverRegistered) {
            val filter = IntentFilter().apply {
                addAction(AppConfig.STATUS_ACTION)
                addAction(AppConfig.HISTORY_ACTION)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                registerReceiver(appReceiver, filter, RECEIVER_NOT_EXPORTED)
            } else {
                @Suppress("DEPRECATION")
                registerReceiver(appReceiver, filter)
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
        val session = secretStore.session()
        if (SecretStore.legacyUpgradeRequired(session, DeviceIdentity.username(this))) {
            upgradeLegacySession()
            return
        }
        if (
            session.isPrivate &&
            statusStore.snapshot().state != StatusStore.STATE_AUTH_FAILED &&
            notificationsAllowed()
        ) {
            startReceiverService()
        }
        showPendingDetail()
    }

    override fun onStop() {
        if (receiverRegistered) {
            unregisterReceiver(appReceiver)
            receiverRegistered = false
        }
        super.onStop()
    }

    override fun onDestroy() {
        historyStore.close()
        super.onDestroy()
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
            setPadding(dp(20), dp(24), dp(20), dp(36))
            setBackgroundColor(Color.rgb(245, 247, 251))
        }
        root.addView(text("AgentWatch", 31f, bold = true).apply { setTextColor(Color.rgb(20, 33, 61)) })
        root.addView(text("你的 AI 任务，送达到你的设备", 16f).apply {
            setTextColor(Color.rgb(77, 91, 124))
            setPadding(0, dp(4), 0, dp(18))
        })
        root.addView(buildStatusCard())
        root.addView(space(14))
        authPanel = buildAuthPanel()
        root.addView(authPanel)
        navigation = buildNavigation()
        root.addView(navigation)
        root.addView(space(12))
        pageContainer = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        messagesPage = buildMessagesPage()
        devicesPage = buildDevicesPage()
        settingsPage = buildSettingsPage()
        pageContainer.addView(messagesPage)
        pageContainer.addView(devicesPage)
        pageContainer.addView(settingsPage)
        root.addView(pageContainer)
        showPage(Page.MESSAGES)
        return ScrollView(this).apply {
            isFillViewport = true
            addView(root, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        }
    }

    private fun buildStatusCard(): LinearLayout = card().apply {
        addView(text("实时连接", 14f, bold = true).apply { setTextColor(Color.rgb(77, 91, 124)) })
        statusText = text("未启动", 23f, bold = true).apply {
            setTextColor(Color.rgb(20, 33, 61))
            setPadding(0, dp(6), 0, 0)
        }
        addView(statusText)
        statusDetailText = helpText("登录后会自动连接").apply { setPadding(0, dp(5), 0, 0) }
        addView(statusDetailText)
        lastDeliveryText = text("尚未收到送达回执", 13f).apply {
            setTextColor(Color.rgb(103, 116, 143))
            setPadding(0, dp(8), 0, 0)
        }
        addView(lastDeliveryText)
    }

    private fun buildAuthPanel(): LinearLayout = card().apply {
        addView(sectionTitle("注册或登录"))
        addView(helpText("每个账号都有独立通知通道。新用户需要邀请代码；已有账号直接登录。"))
        usernameInput = input("账号（3–32 位）")
        addView(usernameInput)
        passwordInput = input("密码（至少 12 位）").apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        addView(passwordInput)
        inviteInput = input("新用户邀请代码")
        addView(inviteInput)
        deviceNameInput = input("手机或平板名称").apply { setText(DeviceIdentity.defaultName()) }
        addView(deviceNameInput)
        registerButton = primaryButton("注册并连接") { authenticate(register = true) }
        addView(registerButton)
        loginButton = secondaryButton("已有账号登录") { authenticate(register = false) }
        addView(loginButton)
    }

    private fun buildNavigation(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        visibility = View.GONE
        Page.entries.forEach { page ->
            val label = when (page) {
                Page.MESSAGES -> "消息"
                Page.DEVICES -> "设备"
                Page.SETTINGS -> "设置"
            }
            val nav = Button(this@MainActivity).apply {
                text = label
                isAllCaps = false
                textSize = 15f
                setTypeface(typeface, Typeface.BOLD)
                setOnClickListener { showPage(page) }
                layoutParams = LinearLayout.LayoutParams(0, dp(46), 1f).apply {
                    marginStart = dp(3)
                    marginEnd = dp(3)
                }
            }
            navButtons[page] = nav
            addView(nav)
        }
    }

    private fun buildMessagesPage(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        addView(card().apply {
            addView(sectionTitle("本机历史消息"))
            addView(helpText("正文只保存在此 App 的私有数据库中；服务器仅短期缓存以便断线补发。"))
            searchInput = input("搜索标题、正文或电脑名称")
            searchInput.addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
                override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) = refreshMessages()
                override fun afterTextChanged(s: Editable?) = Unit
            })
            addView(searchInput)
            categoryRow = LinearLayout(this@MainActivity).apply { orientation = LinearLayout.HORIZONTAL }
            addView(HorizontalScrollView(this@MainActivity).apply {
                isHorizontalScrollBarEnabled = false
                addView(categoryRow)
            })
            addView(space(8))
            addView(secondaryButton("清空当前分类") { confirmClearCurrentCategory() })
        })
        addView(space(12))
        messageList = LinearLayout(this@MainActivity).apply { orientation = LinearLayout.VERTICAL }
        addView(messageList)
    }

    private fun buildDevicesPage(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        addView(card().apply {
            addView(sectionTitle("当前移动设备"))
            accountText = text("", 16f, bold = true).apply { setTextColor(Color.rgb(20, 33, 61)) }
            addView(accountText)
            addView(helpText("此设备通过账号的私有 WebSocket 通道接收；其他账号没有读取权限。"))
            addView(primaryButton("重新连接") {
                startReceiverService(forceReconnect = true)
                toast("正在重新连接")
            })
            testButton = secondaryButton("发送一条端到端测试") { sendEndToEndTest() }
            addView(testButton)
        })
        addView(space(12))
        addView(card().apply {
            addView(sectionTitle("已登录电脑"))
            addView(helpText("电脑使用账号密码登录后会显示在这里。撤销后，该电脑将立即失去发送权限。"))
            addView(secondaryButton("刷新电脑列表") { loadComputers() })
            computerList = LinearLayout(this@MainActivity).apply { orientation = LinearLayout.VERTICAL }
            addView(computerList)
        })
    }

    private fun buildSettingsPage(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        addView(card().apply {
            addView(sectionTitle("历史保留"))
            addView(helpText("默认保留 7 天。无论选择多久，每个账号最多保留最近 500 条，避免无限占用手机空间。"))
            val radioGroup = RadioGroup(this@MainActivity).apply { orientation = RadioGroup.VERTICAL }
            listOf(
                1 to "1 天",
                7 to "7 天（默认）",
                30 to "30 天",
                HistorySettings.PERMANENT to "永久（仍最多 500 条）",
            ).forEach { (days, label) ->
                radioGroup.addView(RadioButton(this@MainActivity).apply {
                    id = View.generateViewId()
                    tag = days
                    text = label
                    textSize = 15f
                    isChecked = historySettings.retentionDays() == days
                })
            }
            radioGroup.setOnCheckedChangeListener { group, checkedId ->
                val days = group.findViewById<RadioButton>(checkedId)?.tag as? Int ?: return@setOnCheckedChangeListener
                historySettings.setRetentionDays(days)
                currentAccount().takeIf { it.isNotBlank() }?.let { historyStore.cleanup(it, days) }
                refreshMessages()
                refreshHistorySize()
            }
            addView(radioGroup)
            historySizeText = helpText("")
            addView(historySizeText)
            addView(secondaryButton("清空全部历史") { confirmClearAllHistory() })
        })
        addView(space(12))
        addView(card().apply {
            addView(sectionTitle("后台送达设置"))
            addView(helpText("请允许通知、自启动，并把电池管理设为完全允许后台行为。系统选项需要你亲自确认。"))
            addView(primaryButton("打开通知设置") { BackgroundSettings.openNotificationSettings(this@MainActivity) })
            addView(secondaryButton("打开自启动设置") { BackgroundSettings.openAutoStartSettings(this@MainActivity) })
            addView(secondaryButton("打开电池设置") { BackgroundSettings.openBatterySettings(this@MainActivity) })
        })
        addView(space(12))
        addView(card().apply {
            addView(sectionTitle("账号"))
            addView(helpText("登录凭据使用 Android Keystore 加密；历史正文不额外加密，但仅位于 App 私有目录且禁止备份。"))
            logoutButton = secondaryButton("退出并撤销此设备") { askLogoutHistoryChoice() }
            addView(logoutButton)
            addView(helpText("版本 ${BuildConfig.VERSION_NAME} · 私有 WebSocket 通道"))
        })
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

    private fun upgradeLegacySession() {
        if (upgradeInFlight) return
        val appToken = secretStore.get(SecretStore.APP_TOKEN)
        if (appToken.isBlank()) return
        upgradeInFlight = true
        statusStore.update(StatusStore.STATE_CONNECTING, "正在把旧会话升级到账号私有通道")
        refreshStatus()
        registrationClient.upgradeSession(appToken) { result ->
            runOnUiThread {
                upgradeInFlight = false
                result.onSuccess {
                    saveSession(it)
                    toast("已升级到账号私有通知通道")
                }.onFailure { error ->
                    statusStore.update(StatusStore.STATE_AUTH_FAILED, "旧会话升级失败，请使用账号密码重新登录")
                    refreshSession()
                    toast(error.message ?: "会话升级失败")
                }
            }
        }
    }

    private fun saveSession(session: RegistrationClient.AuthSession) {
        val previous = secretStore.session()
        val changedChannel = previous.username != session.username || previous.ntfyTopic != session.ntfyTopic
        if (changedChannel) {
            CursorStore(this).reset()
            EventDedupeStore(this).clear()
            AckOutbox(this).clear()
        }
        secretStore.saveSession(session.toSecretSession())
        DeviceIdentity.setUsername(this, session.username)
        logoutStateStore.clear()
        passwordInput.text.clear()
        inviteInput.text.clear()
        statusStore.update(StatusStore.STATE_CONNECTING, "正在连接账号私有通道")
        refreshSession()
        ensurePermissionAndStart()
    }

    private fun showPage(page: Page) {
        selectedPage = page
        messagesPage.visibility = if (page == Page.MESSAGES) View.VISIBLE else View.GONE
        devicesPage.visibility = if (page == Page.DEVICES) View.VISIBLE else View.GONE
        settingsPage.visibility = if (page == Page.SETTINGS) View.VISIBLE else View.GONE
        navButtons.forEach { (candidate, button) -> styleTab(button, candidate == page) }
        when (page) {
            Page.MESSAGES -> refreshMessages()
            Page.DEVICES -> loadComputers()
            Page.SETTINGS -> refreshHistorySize()
        }
    }

    private fun refreshMessages() {
        if (!::messageList.isInitialized || !secretStore.session().isPrivate) return
        rebuildCategoryRow()
        val account = currentAccount()
        historyStore.cleanup(account, historySettings.retentionDays())
        val entries = historyStore.entries(account, selectedSource, searchInput.text.toString())
        messageList.removeAllViews()
        if (entries.isEmpty()) {
            messageList.addView(card().apply { addView(helpText("当前分类还没有历史消息。")) })
        } else {
            entries.forEach { entry ->
                messageList.addView(historyRow(entry))
                messageList.addView(space(8))
            }
        }
        refreshHistorySize()
        showPendingDetail()
    }

    private fun rebuildCategoryRow() {
        categoryRow.removeAllViews()
        val categories = listOf<Pair<NtfyMessage.Source?, String>>(null to "全部") +
            NtfyMessage.Source.entries.map { source ->
                source to if (source == NtfyMessage.Source.OTHER) "其他" else source.displayName
            }
        categories.forEach { (source, label) ->
            val selected = source == selectedSource
            categoryRow.addView(Button(this).apply {
                text = label
                isAllCaps = false
                textSize = 13f
                setTextColor(if (selected) Color.WHITE else Color.rgb(49, 92, 245))
                background = pillBackground(selected)
                setOnClickListener {
                    selectedSource = source
                    refreshMessages()
                }
                layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(42)).apply {
                    marginEnd = dp(7)
                }
            })
        }
    }

    private fun historyRow(entry: HistoryStore.Entry): LinearLayout = card().apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.TOP
        setOnClickListener { showHistoryDetail(entry) }
        addView(ImageView(this@MainActivity).apply {
            setImageResource(SourcePresentation.largeIcon(entry.source) ?: SourcePresentation.smallIcon(entry.source))
            scaleType = ImageView.ScaleType.CENTER_CROP
            layoutParams = LinearLayout.LayoutParams(dp(44), dp(44)).apply { marginEnd = dp(12) }
        })
        addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            addView(text(entry.title, 16f, bold = true).apply { setTextColor(Color.rgb(20, 33, 61)) })
            val origin = entry.computerName.ifBlank { "未知电脑" }
            addView(text("${entry.source.displayName} · $origin · ${formatTime(entry.receivedAt)}", 12f).apply {
                setTextColor(Color.rgb(103, 116, 143))
                setPadding(0, dp(3), 0, dp(5))
            })
            addView(text(entry.body.lineSequence().firstOrNull().orEmpty().take(180), 14f).apply {
                setTextColor(Color.rgb(77, 91, 124))
                maxLines = 3
            })
        })
    }

    private fun showHistoryDetail(entry: HistoryStore.Entry) {
        val origin = entry.computerName.ifBlank { "未知电脑" }
        AlertDialog.Builder(this)
            .setTitle(entry.title)
            .setMessage("${entry.source.displayName} · $origin\n${formatTime(entry.receivedAt)}\n\n${entry.body}")
            .setPositiveButton("关闭", null)
            .setNegativeButton("删除") { _, _ ->
                historyStore.deleteOne(entry.account, entry.eventId)
                refreshMessages()
            }
            .show()
    }

    private fun showPendingDetail() {
        if (pendingEventId.isBlank() || !secretStore.session().isPrivate || !::messageList.isInitialized) return
        val entry = historyStore.find(currentAccount(), pendingEventId) ?: return
        pendingEventId = ""
        intent.removeExtra(EXTRA_EVENT_ID)
        showHistoryDetail(entry)
    }

    private fun confirmClearCurrentCategory() {
        val label = selectedSource?.displayName ?: "全部"
        AlertDialog.Builder(this)
            .setTitle("清空${label}历史？")
            .setMessage("此操作只删除当前账号在本机保存的消息，无法恢复。")
            .setPositiveButton("清空") { _, _ ->
                historyStore.deleteSource(currentAccount(), selectedSource)
                refreshMessages()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun confirmClearAllHistory() {
        AlertDialog.Builder(this)
            .setTitle("清空全部历史？")
            .setMessage("将删除当前账号在本机保存的所有消息。")
            .setPositiveButton("清空") { _, _ ->
                historyStore.deleteSource(currentAccount(), null)
                refreshMessages()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun loadComputers() {
        if (!::computerList.isInitialized || computersInFlight || selectedPage != Page.DEVICES) return
        val token = secretStore.session().appToken
        if (token.isBlank()) return
        computersInFlight = true
        computerList.removeAllViews()
        computerList.addView(helpText("正在读取电脑列表…"))
        registrationClient.listComputers(token) { result ->
            runOnUiThread {
                computersInFlight = false
                computerList.removeAllViews()
                result.onSuccess { computers ->
                    if (computers.isEmpty()) {
                        computerList.addView(helpText("尚无电脑登录。电脑安装完成后使用账号密码登录即可。"))
                    } else {
                        computers.forEach { computer -> computerList.addView(computerRow(computer)) }
                    }
                }.onFailure { error ->
                    computerList.addView(helpText(error.message ?: "无法读取电脑列表"))
                }
            }
        }
    }

    private fun computerRow(computer: RegistrationClient.Computer): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(0, dp(10), 0, dp(5))
        addView(text(computer.name, 16f, bold = true).apply { setTextColor(Color.rgb(20, 33, 61)) })
        val seen = computer.lastSeenAt.takeIf { it > 0 }?.times(1000L)?.let(::formatTime) ?: "尚未发送"
        addView(helpText("${computer.platform} · 最近活动 $seen"))
        addView(secondaryButton("撤销这台电脑") { confirmRevokeComputer(computer) })
    }

    private fun confirmRevokeComputer(computer: RegistrationClient.Computer) {
        AlertDialog.Builder(this)
            .setTitle("撤销 ${computer.name}？")
            .setMessage("撤销后，这台电脑将不能再向你的设备发送消息，需要重新使用账号密码登录。")
            .setPositiveButton("撤销") { _, _ -> revokeComputer(computer) }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun revokeComputer(computer: RegistrationClient.Computer) {
        registrationClient.revokeComputer(secretStore.session().appToken, computer.id) { result ->
            runOnUiThread {
                result.onSuccess {
                    toast("已撤销 ${computer.name}")
                    loadComputers()
                }.onFailure { error -> toast(error.message ?: "撤销失败") }
            }
        }
    }

    private fun ensurePermissionAndStart() {
        if (notificationsAllowed()) {
            startReceiverService()
            return
        }
        if (runtimeNotificationPermissionGranted()) {
            statusStore.update(StatusStore.STATE_PERMISSION_REQUIRED, "系统通知总开关已关闭")
            toast("请在设置中开启 AgentWatch 通知")
            refreshStatus()
            return
        }
        startAfterPermission = true
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQUEST_NOTIFICATIONS)
        }
    }

    private fun startReceiverService(forceReconnect: Boolean = false) {
        if (!secretStore.session().isPrivate) return
        if (!notificationsAllowed()) {
            ensurePermissionAndStart()
            return
        }
        val serviceIntent = Intent(this, WatchService::class.java)
        if (forceReconnect) serviceIntent.action = AppConfig.ACTION_RECONNECT
        try {
            startForegroundService(serviceIntent)
        } catch (_: Exception) {
            statusStore.update(StatusStore.STATE_ERROR, "系统阻止了后台服务，请检查自启动和电池设置")
        }
    }

    private fun sendEndToEndTest() {
        if (statusStore.snapshot().state != StatusStore.STATE_CONNECTED) {
            toast("请等待显示“已连接”后再测试")
            return
        }
        val token = secretStore.session().appToken
        if (token.isBlank()) {
            toast("请重新登录")
            return
        }
        testButton.isEnabled = false
        registrationClient.sendTest(token) { result ->
            runOnUiThread {
                testButton.isEnabled = true
                result.onSuccess { toast("测试已发出，等待一条 WebSocket 通知") }
                    .onFailure { error -> toast(error.message ?: "测试发送失败") }
            }
        }
    }

    private fun askLogoutHistoryChoice() {
        AlertDialog.Builder(this)
            .setTitle("退出登录")
            .setMessage("退出会撤销这台移动设备的服务器凭据。请选择如何处理本机历史。")
            .setPositiveButton("保留历史") { _, _ -> logout(deleteHistory = false) }
            .setNegativeButton("同时删除历史") { _, _ -> logout(deleteHistory = true) }
            .setNeutralButton("取消", null)
            .show()
    }

    private fun logout(deleteHistory: Boolean) {
        val appToken = secretStore.session().appToken
        if (appToken.isBlank()) {
            clearLocalSession(deleteHistory)
            return
        }
        try {
            logoutStateStore.markPending(deleteHistory)
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
            clearLocalSession(logoutStateStore.deleteHistory())
            if (userInitiated) toast("已退出登录")
            return
        }
        logoutRequestInFlight = true
        if (::logoutButton.isInitialized) logoutButton.isEnabled = false
        registrationClient.logout(appToken) { result ->
            runOnUiThread {
                logoutRequestInFlight = false
                if (::logoutButton.isInitialized) logoutButton.isEnabled = true
                result.onSuccess {
                    clearLocalSession(logoutStateStore.deleteHistory())
                    toast("此设备的服务器凭据已撤销")
                }.onFailure { error ->
                    statusStore.update(StatusStore.STATE_ERROR, "服务器尚未确认退出；再次打开应用会自动重试")
                    toast(error.message ?: "服务器尚未确认退出，请稍后重试")
                }
            }
        }
    }

    private fun clearLocalSession(deleteHistory: Boolean) {
        val account = currentAccount()
        stopService(Intent(this, WatchService::class.java))
        if (deleteHistory && account.isNotBlank()) historyStore.deleteSource(account, null)
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
        if (!::authPanel.isInitialized) return
        val session = secretStore.session()
        val authenticationFailed = statusStore.snapshot().state == StatusStore.STATE_AUTH_FAILED
        authPanel.visibility = if (session.isPrivate && !authenticationFailed) View.GONE else View.VISIBLE
        navigation.visibility = if (session.isPrivate) View.VISIBLE else View.GONE
        pageContainer.visibility = if (session.isPrivate) View.VISIBLE else View.GONE
        if (session.isPrivate) {
            accountText.text = getString(R.string.account_and_device, session.username, DeviceIdentity.defaultName())
            refreshMessages()
            if (selectedPage == Page.DEVICES) loadComputers()
        }
        refreshStatus()
    }

    private fun refreshStatus() {
        if (!::statusText.isInitialized) return
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
            if (secretStore.session().isPrivate) "等待接收服务状态" else "登录后会自动连接"
        }
        lastDeliveryText.text = when {
            snapshot.lastAcknowledgedAt > 0L -> "服务器已收到送达回执：${formatTime(snapshot.lastAcknowledgedAt)}"
            snapshot.lastReceivedAt > 0L -> "最近已显示通知：${formatTime(snapshot.lastReceivedAt)}"
            else -> "尚未收到送达回执"
        }
    }

    private fun refreshHistorySize() {
        if (!::historySizeText.isInitialized) return
        val bytes = historyStore.databaseSizeBytes()
        historySizeText.text = "本机历史数据库占用：${formatBytes(bytes)}"
    }

    private fun currentAccount(): String = secretStore.session().username.ifBlank { DeviceIdentity.username(this) }

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

    private fun styleTab(button: Button, selected: Boolean) {
        button.setTextColor(if (selected) Color.WHITE else Color.rgb(49, 92, 245))
        button.background = pillBackground(selected)
    }

    private fun pillBackground(selected: Boolean): GradientDrawable = GradientDrawable().apply {
        setColor(if (selected) Color.rgb(49, 92, 245) else Color.rgb(237, 241, 255))
        cornerRadius = dp(12).toFloat()
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
        setPadding(0, 0, 0, dp(10))
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
        background = pillBackground(primary)
        setOnClickListener { action() }
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(50)).apply { topMargin = dp(6) }
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

    private fun formatBytes(bytes: Long): String = when {
        bytes < 1024 -> "$bytes B"
        bytes < 1024 * 1024 -> String.format(Locale.getDefault(), "%.1f KB", bytes / 1024.0)
        else -> String.format(Locale.getDefault(), "%.1f MB", bytes / (1024.0 * 1024.0))
    }

    private fun toast(message: String) = Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    private fun dp(value: Int): Int = (value * resources.displayMetrics.density + 0.5f).toInt()

    companion object {
        const val EXTRA_EVENT_ID = "event_id"
        private const val REQUEST_NOTIFICATIONS = 2001
    }
}
