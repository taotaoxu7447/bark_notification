package io.github.taotaoxu7447.agentwatch

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

class HistoryStore(private val context: Context) : SQLiteOpenHelper(context, DATABASE_NAME, null, DATABASE_VERSION) {
    enum class InsertResult { INSERTED, EXISTS, DELETED }

    data class Entry(
        val account: String,
        val eventId: String,
        val source: NtfyMessage.Source,
        val computerId: String,
        val computerName: String,
        val title: String,
        val body: String,
        val receivedAt: Long,
    )

    override fun onCreate(database: SQLiteDatabase) {
        database.execSQL(
            """
            CREATE TABLE messages (
                account TEXT NOT NULL,
                event_id TEXT NOT NULL,
                source TEXT NOT NULL,
                computer_id TEXT NOT NULL,
                computer_name TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                received_at INTEGER NOT NULL,
                PRIMARY KEY(account, event_id)
            )
            """.trimIndent(),
        )
        database.execSQL("CREATE INDEX messages_account_time ON messages(account, received_at DESC)")
        database.execSQL("CREATE INDEX messages_account_source_time ON messages(account, source, received_at DESC)")
        database.execSQL(
            """
            CREATE TABLE deleted_events (
                account TEXT NOT NULL,
                event_id TEXT NOT NULL,
                deleted_at INTEGER NOT NULL,
                PRIMARY KEY(account, event_id)
            )
            """.trimIndent(),
        )
    }

    override fun onUpgrade(database: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit

    @Synchronized
    fun insert(account: String, message: NtfyMessage): InsertResult {
        require(account.isNotBlank() && message.eventKey.isNotBlank())
        val database = writableDatabase
        database.beginTransaction()
        try {
            pruneTombstones(database, System.currentTimeMillis())
            if (isDeleted(database, account, message.eventKey)) {
                database.setTransactionSuccessful()
                return InsertResult.DELETED
            }
            val receivedAt = when {
                message.sentAt > 0L -> message.sentAt * 1000L
                message.time > 0L -> message.time * 1000L
                else -> System.currentTimeMillis()
            }
            val row = ContentValues().apply {
                put("account", account)
                put("event_id", message.eventKey)
                put("source", message.source.key)
                put("computer_id", message.computerId)
                put("computer_name", message.computerName)
                put("title", message.title.ifBlank { "${message.source.displayName} 任务提醒" })
                put("body", message.message)
                put("received_at", receivedAt)
            }
            val inserted = database.insertWithOnConflict("messages", null, row, SQLiteDatabase.CONFLICT_IGNORE) != -1L
            database.setTransactionSuccessful()
            return if (inserted) InsertResult.INSERTED else InsertResult.EXISTS
        } finally {
            database.endTransaction()
        }
    }

    @Synchronized
    fun cleanup(account: String, retentionDays: Int, nowMillis: Long = System.currentTimeMillis()) {
        val database = writableDatabase
        HistorySettings.cutoffMillis(nowMillis, retentionDays)?.let { cutoff ->
            database.delete("messages", "account = ? AND received_at < ?", arrayOf(account, cutoff.toString()))
        }
        database.execSQL(
            """
            DELETE FROM messages
            WHERE account = ? AND event_id NOT IN (
                SELECT event_id FROM messages WHERE account = ?
                ORDER BY received_at DESC, event_id DESC LIMIT ?
            )
            """.trimIndent(),
            arrayOf<Any>(account, account, HistorySettings.MAX_MESSAGES_PER_ACCOUNT),
        )
        pruneTombstones(database, nowMillis)
    }

    @Synchronized
    fun cleanupAll(retentionDays: Int, nowMillis: Long = System.currentTimeMillis()) {
        val accounts = readableDatabase.rawQuery("SELECT DISTINCT account FROM messages", null).use { cursor ->
            buildList { while (cursor.moveToNext()) add(cursor.getString(0)) }
        }
        accounts.forEach { account -> cleanup(account, retentionDays, nowMillis) }
        pruneTombstones(writableDatabase, nowMillis)
    }

    @Synchronized
    fun entries(account: String, source: NtfyMessage.Source?, search: String): List<Entry> {
        val clauses = mutableListOf("account = ?")
        val arguments = mutableListOf(account)
        if (source != null) {
            clauses += "source = ?"
            arguments += source.key
        }
        val normalizedSearch = search.trim()
        if (normalizedSearch.isNotBlank()) {
            clauses += "(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' OR computer_name LIKE ? ESCAPE '\\')"
            val term = "%${escapeLike(normalizedSearch)}%"
            repeat(3) { arguments += term }
        }
        return readableDatabase.query(
            "messages",
            COLUMNS,
            clauses.joinToString(" AND "),
            arguments.toTypedArray(),
            null,
            null,
            "received_at DESC, event_id DESC",
            HistorySettings.MAX_MESSAGES_PER_ACCOUNT.toString(),
        ).use { cursor -> buildList { while (cursor.moveToNext()) add(cursor.toEntry()) } }
    }

    @Synchronized
    fun find(account: String, eventId: String): Entry? = readableDatabase.query(
        "messages",
        COLUMNS,
        "account = ? AND event_id = ?",
        arrayOf(account, eventId),
        null,
        null,
        null,
        "1",
    ).use { cursor -> if (cursor.moveToFirst()) cursor.toEntry() else null }

    @Synchronized
    fun deleteOne(account: String, eventId: String, remember: Boolean = true) {
        val database = writableDatabase
        database.beginTransaction()
        try {
            if (remember) tombstone(database, account, "event_id = ?", arrayOf(eventId))
            database.delete("messages", "account = ? AND event_id = ?", arrayOf(account, eventId))
            database.setTransactionSuccessful()
        } finally {
            database.endTransaction()
        }
    }

    @Synchronized
    fun deleteSource(account: String, source: NtfyMessage.Source?) {
        val database = writableDatabase
        val condition = if (source == null) "1 = 1" else "source = ?"
        val arguments = if (source == null) emptyArray() else arrayOf(source.key)
        database.beginTransaction()
        try {
            tombstone(database, account, condition, arguments)
            val where = "account = ? AND $condition"
            database.delete("messages", where, arrayOf(account, *arguments))
            database.setTransactionSuccessful()
        } finally {
            database.endTransaction()
        }
    }

    fun databaseSizeBytes(): Long {
        val base = context.getDatabasePath(DATABASE_NAME)
        return listOf(base, java.io.File(base.path + "-wal"), java.io.File(base.path + "-shm"))
            .sumOf { file -> file.takeIf { it.exists() }?.length() ?: 0L }
    }

    private fun tombstone(database: SQLiteDatabase, account: String, condition: String, arguments: Array<String>) {
        val sql = """
            INSERT OR REPLACE INTO deleted_events(account, event_id, deleted_at)
            SELECT account, event_id, ? FROM messages WHERE account = ? AND $condition
        """.trimIndent()
        database.execSQL(sql, arrayOf<Any>(System.currentTimeMillis(), account, *arguments))
    }

    private fun isDeleted(database: SQLiteDatabase, account: String, eventId: String): Boolean =
        database.rawQuery(
            "SELECT 1 FROM deleted_events WHERE account = ? AND event_id = ? LIMIT 1",
            arrayOf(account, eventId),
        ).use(Cursor::moveToFirst)

    private fun pruneTombstones(database: SQLiteDatabase, nowMillis: Long) {
        database.delete(
            "deleted_events",
            "deleted_at < ?",
            arrayOf((nowMillis - TOMBSTONE_TTL_MILLIS).toString()),
        )
    }

    private fun Cursor.toEntry(): Entry = Entry(
        account = getString(getColumnIndexOrThrow("account")),
        eventId = getString(getColumnIndexOrThrow("event_id")),
        source = NtfyMessage.sourceForKey(getString(getColumnIndexOrThrow("source"))),
        computerId = getString(getColumnIndexOrThrow("computer_id")),
        computerName = getString(getColumnIndexOrThrow("computer_name")),
        title = getString(getColumnIndexOrThrow("title")),
        body = getString(getColumnIndexOrThrow("body")),
        receivedAt = getLong(getColumnIndexOrThrow("received_at")),
    )

    companion object {
        private const val DATABASE_NAME = "agentwatch_history.db"
        private const val DATABASE_VERSION = 1
        private const val TOMBSTONE_TTL_MILLIS = 24L * 60L * 60L * 1000L
        private val COLUMNS = arrayOf(
            "account",
            "event_id",
            "source",
            "computer_id",
            "computer_name",
            "title",
            "body",
            "received_at",
        )

        internal fun escapeLike(value: String): String = value
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
    }
}
