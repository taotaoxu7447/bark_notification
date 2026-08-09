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
import android.content.res.ColorStateList
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.os.Build
import android.os.Bundle
import android.text.Editable
import android.text.InputType
import android.text.TextUtils
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
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
    private lateinit var mainScroll: ScrollView
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
    private lateinit var statusDot: View
    private lateinit var statusText: TextView
    private lateinit var statusDetailText: TextView
    private lateinit var lastDeliveryText: TextView
    private lateinit var testButton: Button
    private lateinit var logoutButton: Button
    private lateinit var searchInput: EditText
    private lateinit var categoryRow: LinearLayout
    private lateinit var messageList: LinearLayout
    private lateinit var messageCountText: TextView
    private lateinit var computerList: LinearLayout
    private lateinit var historySizeText: TextView
    private val navButtons = mutableMapOf<Page, TextView>()
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
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility =
                window.decorView.systemUiVisibility or View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
        }
        secretStore = SecretStore(this)
        statusStore = StatusStore(this)
        registrationClient = RegistrationClient(this)
        logoutStateStore = LogoutStateStore(this)
        historyStore = HistoryStore(this)
        historySettings = HistorySettings(this)
        historyStore.cleanupAll(historySettings.retentionDays())
        NotificationRenderer(this).createChannels()
        pendingEventId = intent.getStringExtra(EXTRA_EVENT_ID).orEmpty()
        selectedPage = savedInstanceState?.getString(STATE_PAGE)
            ?.let { value -> Page.entries.firstOrNull { it.name == value } }
            ?: Page.MESSAGES
        selectedSource = savedInstanceState?.getString(STATE_SOURCE)
            ?.let(NtfyMessage::sourceForKey)
        setContentView(buildContent())
        savedInstanceState?.getString(STATE_SEARCH).orEmpty().takeIf { it.isNotBlank() }?.let(searchInput::setText)
        refreshSession()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putString(STATE_PAGE, selectedPage.name)
        outState.putString(STATE_SOURCE, selectedSource?.key)
        if (::searchInput.isInitialized) outState.putString(STATE_SEARCH, searchInput.text.toString())
        super.onSaveInstanceState(outState)
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
        val shell = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(COLOR_BACKGROUND)
        }
        shell.setOnApplyWindowInsetsListener { view, insets ->
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                val bars = insets.getInsets(android.view.WindowInsets.Type.systemBars())
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            } else {
                @Suppress("DEPRECATION")
                view.setPadding(
                    insets.systemWindowInsetLeft,
                    insets.systemWindowInsetTop,
                    insets.systemWindowInsetRight,
                    insets.systemWindowInsetBottom,
                )
            }
            insets
        }

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(18), dp(18), dp(30))
        }
        root.addView(buildBrandHeader())
        root.addView(space(18))
        root.addView(buildStatusCard())
        root.addView(space(16))
        authPanel = buildAuthPanel()
        root.addView(authPanel)
        pageContainer = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        messagesPage = buildMessagesPage()
        devicesPage = buildDevicesPage()
        settingsPage = buildSettingsPage()
        pageContainer.addView(messagesPage)
        pageContainer.addView(devicesPage)
        pageContainer.addView(settingsPage)
        root.addView(pageContainer)

        val availableWidth = resources.displayMetrics.widthPixels - dp(24)
        val contentWidth = minOf(availableWidth, dp(760)).coerceAtLeast(dp(280))
        val centeredContent = FrameLayout(this).apply {
            addView(
                root,
                FrameLayout.LayoutParams(
                    contentWidth,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    Gravity.TOP or Gravity.CENTER_HORIZONTAL,
                ),
            )
        }
        mainScroll = ScrollView(this).apply {
            isFillViewport = true
            clipToPadding = false
            overScrollMode = View.OVER_SCROLL_IF_CONTENT_SCROLLS
            addView(
                centeredContent,
                ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT),
            )
        }
        shell.addView(mainScroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        navigation = buildNavigation()
        shell.addView(
            navigation,
            LinearLayout.LayoutParams(contentWidth, dp(70)).apply { gravity = Gravity.CENTER_HORIZONTAL },
        )
        showPage(selectedPage)
        return shell
    }

    private fun buildBrandHeader(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        addView(FrameLayout(this@MainActivity).apply {
            background = roundedBackground(COLOR_NAVY, 17)
            clipToOutline = true
            addView(ImageView(this@MainActivity).apply {
                setImageResource(R.drawable.ic_launcher_agentwatch_v2)
                scaleType = ImageView.ScaleType.CENTER_CROP
            }, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        }, LinearLayout.LayoutParams(dp(54), dp(54)).apply { marginEnd = dp(13) })
        addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            addView(text("AgentWatch", 26f, bold = true).apply {
                setTextColor(COLOR_TEXT_PRIMARY)
                includeFontPadding = false
                letterSpacing = 0.01f
            })
            addView(text("AI 任务完成，立即送达", 13f).apply {
                setTextColor(COLOR_TEXT_SECONDARY)
                setPadding(0, dp(4), 0, 0)
            })
        })
        addView(text("私有通道", 12f, bold = true).apply {
            setTextColor(COLOR_BLUE)
            gravity = Gravity.CENTER
            background = roundedBackground(COLOR_BLUE_SOFT, 10)
            setPadding(dp(10), dp(7), dp(10), dp(7))
        })
    }

    private fun buildStatusCard(): LinearLayout = darkCard().apply {
        addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            statusDot = View(this@MainActivity).apply { background = dotBackground(COLOR_MUTED_LIGHT) }
            addView(statusDot, LinearLayout.LayoutParams(dp(9), dp(9)).apply { marginEnd = dp(8) })
            addView(text("实时连接", 13f, bold = true).apply {
                setTextColor(COLOR_ON_DARK_MUTED)
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            })
            addView(text("WebSocket", 11f, bold = true).apply {
                setTextColor(Color.WHITE)
                gravity = Gravity.CENTER
                background = roundedBackground(Color.argb(35, 255, 255, 255), 9)
                setPadding(dp(9), dp(5), dp(9), dp(5))
            })
        })
        statusText = text("未启动", 27f, bold = true).apply {
            setTextColor(Color.WHITE)
            includeFontPadding = false
            setPadding(0, dp(12), 0, 0)
        }
        addView(statusText)
        statusDetailText = text("登录后会自动连接", 14f).apply {
            setTextColor(COLOR_ON_DARK)
            setLineSpacing(0f, 1.12f)
            setPadding(0, dp(7), 0, dp(15))
        }
        addView(statusDetailText)
        addView(divider(Color.argb(38, 255, 255, 255)))
        lastDeliveryText = text("尚未收到送达回执", 13f).apply {
            setTextColor(COLOR_ON_DARK_MUTED)
            setPadding(0, dp(13), 0, 0)
        }
        addView(lastDeliveryText)
    }

    private fun buildAuthPanel(): LinearLayout = card().apply {
        addView(eyebrow("开始使用"))
        addView(sectionTitle("连接你的私有通知通道"))
        addView(helpText("一个账号对应一条独立通道。已有账号直接登录，新用户填写邀请代码后注册。"))
        usernameInput = input("3–32 位字母、数字或 . _ -")
        addView(field("账号", usernameInput))
        passwordInput = input("至少 12 位").apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        addView(field("密码", passwordInput))
        inviteInput = input("仅注册新账号时填写")
        addView(field("邀请代码", inviteInput))
        deviceNameInput = input("例如：客厅平板").apply { setText(DeviceIdentity.defaultName()) }
        addView(field("设备名称", deviceNameInput))
        registerButton = primaryButton("注册并连接") { authenticate(register = true) }
        addView(registerButton)
        loginButton = secondaryButton("已有账号登录") { authenticate(register = false) }
        addView(loginButton)
        addView(text("账号凭据会使用 Android Keystore 加密保存在此设备。", 12f).apply {
            setTextColor(COLOR_TEXT_MUTED)
            gravity = Gravity.CENTER
            setPadding(0, dp(14), 0, 0)
        })
    }

    private fun buildNavigation(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER
        visibility = View.GONE
        setPadding(dp(8), dp(7), dp(8), dp(7))
        background = roundedBackground(Color.WHITE, 22, COLOR_BORDER)
        elevation = dp(10).toFloat()
        Page.entries.forEach { page ->
            val label = when (page) {
                Page.MESSAGES -> "消息"
                Page.DEVICES -> "设备"
                Page.SETTINGS -> "设置"
            }
            val iconResource = when (page) {
                Page.MESSAGES -> R.drawable.ic_ui_messages
                Page.DEVICES -> R.drawable.ic_ui_devices
                Page.SETTINGS -> R.drawable.ic_ui_settings
            }
            val nav = TextView(this@MainActivity).apply {
                text = label
                textSize = 12f
                gravity = Gravity.CENTER
                setTypeface(typeface, Typeface.BOLD)
                setCompoundDrawablesWithIntrinsicBounds(0, iconResource, 0, 0)
                compoundDrawablePadding = dp(4)
                setOnClickListener { showPage(page) }
                layoutParams = LinearLayout.LayoutParams(0, dp(56), 1f).apply {
                    marginStart = dp(2)
                    marginEnd = dp(2)
                }
            }
            navButtons[page] = nav
            addView(nav)
        }
    }

    private fun buildMessagesPage(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        addView(card().apply {
            addView(eyebrow("消息中心"))
            addView(LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                addView(sectionTitle("本机历史消息").apply {
                    layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                })
                messageCountText = countBadge("0 条")
                addView(messageCountText)
            })
            addView(helpText("消息正文只保存在此设备；服务器仅短期缓存，用于断线补发。"))
            searchInput = input("搜索标题、正文或电脑名称")
            searchInput.setCompoundDrawablesWithIntrinsicBounds(R.drawable.ic_ui_search, 0, 0, 0)
            searchInput.compoundDrawablePadding = dp(10)
            searchInput.compoundDrawableTintList = ColorStateList.valueOf(COLOR_TEXT_MUTED)
            searchInput.addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
                override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) = refreshMessages()
                override fun afterTextChanged(s: Editable?) = Unit
            })
            addView(searchInput)
            categoryRow = LinearLayout(this@MainActivity).apply { orientation = LinearLayout.HORIZONTAL }
            addView(HorizontalScrollView(this@MainActivity).apply {
                isHorizontalScrollBarEnabled = false
                overScrollMode = View.OVER_SCROLL_NEVER
                addView(categoryRow)
            })
            addView(space(10))
            addView(dangerButton("清空当前分类") { confirmClearCurrentCategory() })
        })
        addView(space(14))
        messageList = LinearLayout(this@MainActivity).apply { orientation = LinearLayout.VERTICAL }
        addView(messageList)
    }

    private fun buildDevicesPage(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        addView(card().apply {
            addView(eyebrow("接收端"))
            addView(sectionTitle("当前移动设备"))
            accountText = text("", 17f, bold = true).apply { setTextColor(COLOR_TEXT_PRIMARY) }
            addView(accountText)
            addView(text("已绑定到账号私有 WebSocket 通道，其他账号无法读取。", 13f).apply {
                setTextColor(COLOR_TEXT_SECONDARY)
                setPadding(0, dp(6), 0, dp(14))
            })
            val reconnectButton = primaryButton("重新连接") {
                startReceiverService(forceReconnect = true)
                toast("正在重新连接")
            }
            testButton = secondaryButton("发送测试") { sendEndToEndTest() }
            addView(LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL
                addView(reconnectButton, LinearLayout.LayoutParams(0, dp(52), 1f).apply { marginEnd = dp(5) })
                addView(testButton, LinearLayout.LayoutParams(0, dp(52), 1f).apply { marginStart = dp(5) })
            })
        })
        addView(space(14))
        addView(card().apply {
            addView(eyebrow("发送端"))
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
            addView(eyebrow("本地存储"))
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
                    setTextColor(COLOR_TEXT_PRIMARY)
                    buttonTintList = radioButtonTint()
                    gravity = Gravity.CENTER_VERTICAL
                    minHeight = dp(52)
                    setPadding(dp(10), 0, dp(10), 0)
                    background = roundedBackground(COLOR_SURFACE_SUBTLE, 12)
                    layoutParams = RadioGroup.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.WRAP_CONTENT,
                    ).apply { bottomMargin = dp(7) }
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
            historySizeText = text("", 13f).apply {
                setTextColor(COLOR_TEXT_SECONDARY)
                background = roundedBackground(COLOR_BLUE_SOFT, 12)
                setPadding(dp(13), dp(11), dp(13), dp(11))
            }
            addView(historySizeText)
            addView(dangerButton("清空全部历史") { confirmClearAllHistory() })
        })
        addView(space(14))
        addView(card().apply {
            addView(eyebrow("系统权限"))
            addView(sectionTitle("后台送达设置"))
            addView(helpText("请允许通知、自启动，并把电池管理设为完全允许后台行为。系统选项需要你亲自确认。"))
            addView(settingRow(R.drawable.ic_ui_notifications, "通知权限", "允许任务完成提醒与手表震动") {
                BackgroundSettings.openNotificationSettings(this@MainActivity)
            })
            addView(space(8))
            addView(settingRow(R.drawable.ic_ui_autostart, "自启动", "重启手机后自动恢复连接") {
                BackgroundSettings.openAutoStartSettings(this@MainActivity)
            })
            addView(space(8))
            addView(settingRow(R.drawable.ic_ui_battery, "电池与后台", "允许长时间保持 WebSocket 连接") {
                BackgroundSettings.openBatterySettings(this@MainActivity)
            })
        })
        addView(space(14))
        addView(card().apply {
            addView(eyebrow("安全"))
            addView(sectionTitle("账号与设备"))
            addView(helpText("登录凭据使用 Android Keystore 加密；历史正文仅保存在 App 私有目录并禁止备份。"))
            logoutButton = dangerButton("退出并撤销此设备") { askLogoutHistoryChoice() }
            addView(logoutButton)
            addView(text("AgentWatch ${BuildConfig.VERSION_NAME}  ·  私有 WebSocket 通道", 12f).apply {
                setTextColor(COLOR_TEXT_MUTED)
                gravity = Gravity.CENTER
                setPadding(0, dp(14), 0, 0)
            })
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
        if (::mainScroll.isInitialized) mainScroll.post { mainScroll.scrollTo(0, 0) }
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
        if (::messageCountText.isInitialized) messageCountText.text = getString(R.string.message_count, entries.size)
        if (entries.isEmpty()) {
            messageList.addView(emptyMessagesView())
        } else {
            entries.forEach { entry ->
                messageList.addView(historyRow(entry))
                messageList.addView(space(10))
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
            categoryRow.addView(TextView(this).apply {
                text = label
                textSize = 13f
                gravity = Gravity.CENTER
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(if (selected) Color.WHITE else COLOR_BLUE)
                background = rippleBackground(
                    roundedBackground(if (selected) COLOR_BLUE else COLOR_BLUE_SOFT, 13),
                    if (selected) Color.argb(45, 255, 255, 255) else Color.argb(28, 49, 92, 245),
                )
                setOnClickListener {
                    selectedSource = source
                    refreshMessages()
                }
                minWidth = dp(56)
                setPadding(dp(16), 0, dp(16), 0)
                layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(48)).apply {
                    marginEnd = dp(8)
                }
            })
        }
    }

    private fun historyRow(entry: HistoryStore.Entry): LinearLayout = card().apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        setOnClickListener { showHistoryDetail(entry) }
        isClickable = true
        isFocusable = true
        background = rippleBackground(roundedBackground(Color.WHITE, 19, COLOR_BORDER), Color.argb(24, 49, 92, 245))
        addView(FrameLayout(this@MainActivity).apply {
            background = roundedBackground(sourceTint(entry.source), 14)
            addView(ImageView(this@MainActivity).apply {
                setImageResource(SourcePresentation.largeIcon(entry.source) ?: SourcePresentation.smallIcon(entry.source))
                scaleType = ImageView.ScaleType.CENTER_INSIDE
                setPadding(dp(5), dp(5), dp(5), dp(5))
            }, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        }, LinearLayout.LayoutParams(dp(50), dp(50)).apply { marginEnd = dp(13) })
        addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            addView(text(entry.title, 16f, bold = true).apply {
                setTextColor(COLOR_TEXT_PRIMARY)
                maxLines = 1
                ellipsize = TextUtils.TruncateAt.END
            })
            val origin = entry.computerName.ifBlank { "未知电脑" }
            addView(text("${entry.source.displayName} · $origin · ${formatTime(entry.receivedAt)}", 12f).apply {
                setTextColor(COLOR_TEXT_MUTED)
                maxLines = 1
                ellipsize = TextUtils.TruncateAt.END
                setPadding(0, dp(4), 0, dp(7))
            })
            addView(text(entry.body.lineSequence().firstOrNull().orEmpty().take(180), 14f).apply {
                setTextColor(COLOR_TEXT_SECONDARY)
                maxLines = 3
                ellipsize = TextUtils.TruncateAt.END
                setLineSpacing(0f, 1.12f)
            })
        })
        addView(text("›", 25f).apply {
            setTextColor(COLOR_TEXT_MUTED)
            gravity = Gravity.CENTER
            setPadding(dp(8), 0, 0, 0)
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
        setPadding(dp(14), dp(14), dp(14), dp(12))
        background = roundedBackground(COLOR_SURFACE_SUBTLE, 14)
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
            topMargin = dp(10)
        }
        addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(iconTile(R.drawable.ic_ui_devices, COLOR_BLUE), LinearLayout.LayoutParams(dp(40), dp(40)).apply {
                marginEnd = dp(11)
            })
            addView(LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                addView(text(computer.name, 16f, bold = true).apply {
                    setTextColor(COLOR_TEXT_PRIMARY)
                    maxLines = 1
                    ellipsize = TextUtils.TruncateAt.END
                })
                val seen = computer.lastSeenAt.takeIf { it > 0 }?.times(1000L)?.let(::formatTime) ?: "尚未发送"
                addView(text("${computer.platform} · 最近活动 $seen", 12f).apply {
                    setTextColor(COLOR_TEXT_MUTED)
                    setPadding(0, dp(4), 0, 0)
                })
            })
        })
        addView(dangerButton("撤销这台电脑") { confirmRevokeComputer(computer) })
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
        val visibility = MainUiLogic.sessionVisibility(session.isPrivate, authenticationFailed)
        authPanel.visibility = if (visibility.showAuthentication) View.VISIBLE else View.GONE
        navigation.visibility = if (visibility.showNavigation) View.VISIBLE else View.GONE
        pageContainer.visibility = if (visibility.showPages) View.VISIBLE else View.GONE
        if (visibility.showPages) {
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
        statusText.setTextColor(Color.WHITE)
        statusDot.background = dotBackground(
            when (snapshot.state) {
                StatusStore.STATE_CONNECTED -> COLOR_SUCCESS
                StatusStore.STATE_CONNECTING, StatusStore.STATE_RECONNECTING -> COLOR_CYAN
                StatusStore.STATE_AUTH_FAILED, StatusStore.STATE_ERROR -> COLOR_DANGER_LIGHT
                StatusStore.STATE_PERMISSION_REQUIRED -> COLOR_WARNING
                else -> COLOR_MUTED_LIGHT
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
        historySizeText.text = getString(R.string.history_database_size, formatBytes(bytes))
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

    private fun styleTab(button: TextView, selected: Boolean) {
        val color = if (selected) Color.WHITE else COLOR_TEXT_MUTED
        button.setTextColor(color)
        button.compoundDrawableTintList = ColorStateList.valueOf(color)
        button.background = rippleBackground(
            roundedBackground(if (selected) COLOR_BLUE else Color.TRANSPARENT, 16),
            if (selected) Color.argb(42, 255, 255, 255) else Color.argb(24, 49, 92, 245),
        )
        button.isSelected = selected
    }

    private fun card(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(19), dp(19), dp(19), dp(19))
        background = roundedBackground(Color.WHITE, 21, COLOR_BORDER)
        elevation = dp(2).toFloat()
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
    }

    private fun darkCard(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(20), dp(19), dp(20), dp(19))
        background = GradientDrawable(
            GradientDrawable.Orientation.TL_BR,
            intArrayOf(COLOR_NAVY, COLOR_BLUE_DARK),
        ).apply { cornerRadius = dp(24).toFloat() }
        elevation = dp(6).toFloat()
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
    }

    private fun sectionTitle(value: String): TextView = text(value, 20f, bold = true).apply {
        setTextColor(COLOR_TEXT_PRIMARY)
        includeFontPadding = false
        setPadding(0, 0, 0, dp(8))
    }

    private fun eyebrow(value: String): TextView = text(value, 12f, bold = true).apply {
        setTextColor(COLOR_BLUE)
        letterSpacing = 0.08f
        setPadding(0, 0, 0, dp(7))
    }

    private fun helpText(value: String): TextView = text(value, 14f).apply {
        setTextColor(COLOR_TEXT_SECONDARY)
        setLineSpacing(0f, 1.15f)
        setPadding(0, 0, 0, dp(12))
    }

    private fun field(label: String, input: EditText): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        addView(text(label, 13f, bold = true).apply {
            setTextColor(COLOR_TEXT_PRIMARY)
            setPadding(dp(2), 0, 0, dp(7))
        })
        addView(input)
    }

    private fun input(hintValue: String): EditText = EditText(this).apply {
        hint = hintValue
        textSize = 15f
        setTextColor(COLOR_TEXT_PRIMARY)
        setHintTextColor(COLOR_TEXT_MUTED)
        setSingleLine(true)
        minHeight = dp(54)
        setPadding(dp(15), dp(12), dp(15), dp(12))
        background = roundedBackground(COLOR_INPUT, 14, COLOR_INPUT_BORDER)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply {
            bottomMargin = dp(12)
        }
    }

    private fun primaryButton(label: String, action: () -> Unit): Button = button(label, true, action)
    private fun secondaryButton(label: String, action: () -> Unit): Button = button(label, false, action)
    private fun dangerButton(label: String, action: () -> Unit): Button = Button(this).apply {
        text = label
        textSize = 14f
        isAllCaps = false
        gravity = Gravity.CENTER
        setTypeface(typeface, Typeface.BOLD)
        setTextColor(COLOR_DANGER)
        minHeight = 0
        minWidth = 0
        stateListAnimator = null
        background = rippleBackground(
            roundedBackground(COLOR_DANGER_SOFT, 14),
            Color.argb(28, 196, 55, 67),
        )
        setOnClickListener { action() }
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(50)).apply { topMargin = dp(8) }
    }

    private fun button(label: String, primary: Boolean, action: () -> Unit): Button = Button(this).apply {
        text = label
        textSize = 15f
        isAllCaps = false
        gravity = Gravity.CENTER
        setTypeface(typeface, Typeface.BOLD)
        setTextColor(if (primary) Color.WHITE else COLOR_BLUE)
        minHeight = 0
        minWidth = 0
        stateListAnimator = null
        background = rippleBackground(
            roundedBackground(if (primary) COLOR_BLUE else COLOR_BLUE_SOFT, 14),
            if (primary) Color.argb(44, 255, 255, 255) else Color.argb(28, 49, 92, 245),
        )
        setOnClickListener { action() }
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52)).apply { topMargin = dp(7) }
    }

    private fun settingRow(iconResource: Int, title: String, subtitle: String, action: () -> Unit): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(13), dp(12), dp(12), dp(12))
            minimumHeight = dp(72)
            isClickable = true
            isFocusable = true
            background = rippleBackground(
                roundedBackground(COLOR_SURFACE_SUBTLE, 14),
                Color.argb(24, 49, 92, 245),
            )
            setOnClickListener { action() }
            addView(iconTile(iconResource, COLOR_BLUE), LinearLayout.LayoutParams(dp(42), dp(42)).apply {
                marginEnd = dp(12)
            })
            addView(LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                addView(text(title, 15f, bold = true).apply { setTextColor(COLOR_TEXT_PRIMARY) })
                addView(text(subtitle, 12f).apply {
                    setTextColor(COLOR_TEXT_MUTED)
                    setPadding(0, dp(3), 0, 0)
                })
            })
            addView(text("›", 24f).apply { setTextColor(COLOR_TEXT_MUTED) })
        }

    private fun emptyMessagesView(): LinearLayout = card().apply {
        gravity = Gravity.CENTER_HORIZONTAL
        setPadding(dp(20), dp(30), dp(20), dp(30))
        addView(ImageView(this@MainActivity).apply {
            setImageResource(R.drawable.ic_ui_empty)
            imageTintList = ColorStateList.valueOf(COLOR_BLUE)
            background = roundedBackground(COLOR_BLUE_SOFT, 24)
            setPadding(dp(14), dp(14), dp(14), dp(14))
        }, LinearLayout.LayoutParams(dp(64), dp(64)))
        addView(text("还没有消息", 17f, bold = true).apply {
            setTextColor(COLOR_TEXT_PRIMARY)
            setPadding(0, dp(14), 0, dp(5))
        })
        addView(text("AI 完成任务后，通知和本机历史会出现在这里。", 13f).apply {
            setTextColor(COLOR_TEXT_MUTED)
            gravity = Gravity.CENTER
        })
    }

    private fun iconTile(iconResource: Int, tint: Int): FrameLayout = FrameLayout(this).apply {
        background = roundedBackground(COLOR_BLUE_SOFT, 12)
        addView(ImageView(this@MainActivity).apply {
            setImageResource(iconResource)
            imageTintList = ColorStateList.valueOf(tint)
            setPadding(dp(10), dp(10), dp(10), dp(10))
        }, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
    }

    private fun countBadge(value: String): TextView = text(value, 12f, bold = true).apply {
        setTextColor(COLOR_BLUE)
        gravity = Gravity.CENTER
        background = roundedBackground(COLOR_BLUE_SOFT, 10)
        setPadding(dp(10), dp(5), dp(10), dp(5))
    }

    private fun roundedBackground(fillColor: Int, radius: Int, strokeColor: Int? = null): GradientDrawable =
        GradientDrawable().apply {
            setColor(fillColor)
            cornerRadius = dp(radius).toFloat()
            if (strokeColor != null) setStroke(dp(1), strokeColor)
        }

    private fun rippleBackground(content: GradientDrawable, rippleColor: Int): RippleDrawable =
        RippleDrawable(ColorStateList.valueOf(rippleColor), content, null)

    private fun dotBackground(color: Int): GradientDrawable = GradientDrawable().apply {
        shape = GradientDrawable.OVAL
        setColor(color)
    }

    private fun divider(color: Int): View = View(this).apply {
        setBackgroundColor(color)
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1))
    }

    private fun radioButtonTint(): ColorStateList = ColorStateList(
        arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()),
        intArrayOf(COLOR_BLUE, COLOR_TEXT_MUTED),
    )

    private fun sourceTint(source: NtfyMessage.Source): Int = when (source) {
        NtfyMessage.Source.CODEX -> Color.rgb(235, 248, 244)
        NtfyMessage.Source.ZCODE -> Color.rgb(241, 239, 255)
        NtfyMessage.Source.KIMI -> Color.rgb(237, 244, 255)
        NtfyMessage.Source.GROK -> Color.rgb(242, 243, 247)
        NtfyMessage.Source.CLAUDE -> Color.rgb(255, 241, 234)
        NtfyMessage.Source.PI -> Color.rgb(245, 239, 255)
        NtfyMessage.Source.OPENCODE -> Color.rgb(234, 248, 246)
        NtfyMessage.Source.OTHER -> COLOR_BLUE_SOFT
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
        private const val STATE_PAGE = "ui_page"
        private const val STATE_SOURCE = "ui_source"
        private const val STATE_SEARCH = "ui_search"
        private val COLOR_BACKGROUND = Color.rgb(244, 246, 251)
        private val COLOR_NAVY = Color.rgb(12, 19, 58)
        private val COLOR_BLUE_DARK = Color.rgb(34, 70, 211)
        private val COLOR_BLUE = Color.rgb(49, 92, 245)
        private val COLOR_BLUE_SOFT = Color.rgb(237, 242, 255)
        private val COLOR_CYAN = Color.rgb(27, 220, 238)
        private val COLOR_SUCCESS = Color.rgb(76, 231, 157)
        private val COLOR_WARNING = Color.rgb(255, 196, 85)
        private val COLOR_DANGER = Color.rgb(190, 52, 66)
        private val COLOR_DANGER_LIGHT = Color.rgb(255, 125, 137)
        private val COLOR_DANGER_SOFT = Color.rgb(255, 239, 241)
        private val COLOR_TEXT_PRIMARY = Color.rgb(20, 30, 57)
        private val COLOR_TEXT_SECONDARY = Color.rgb(75, 88, 119)
        private val COLOR_TEXT_MUTED = Color.rgb(112, 124, 151)
        private val COLOR_ON_DARK = Color.rgb(225, 231, 255)
        private val COLOR_ON_DARK_MUTED = Color.rgb(184, 197, 241)
        private val COLOR_MUTED_LIGHT = Color.rgb(166, 179, 222)
        private val COLOR_BORDER = Color.rgb(226, 231, 241)
        private val COLOR_INPUT_BORDER = Color.rgb(207, 216, 234)
        private val COLOR_INPUT = Color.rgb(248, 250, 254)
        private val COLOR_SURFACE_SUBTLE = Color.rgb(247, 249, 253)
    }
}
