import 'package:path/path.dart' as p;
import 'package:sqflite/sqflite.dart';

/// 离线动作。字段与 `POST /api/dm/run/action` 和 `/run/sync` 对齐。
class PendingAction {
  const PendingAction({
    required this.id,
    required this.action,
    required this.at,
    required this.expectedVersion,
    this.minutes,
    this.mode,
  });

  final String id;
  final String action;
  final String at;
  final int expectedVersion;
  final int? minutes;
  final String? mode;

  Map<String, Object?> toMap() => {
        'id': id,
        'action': action,
        'at': at,
        'expected_version': expectedVersion,
        'minutes': minutes,
        'mode': mode,
      };

  Map<String, Object?> toRequest() => {
        'client_event_id': id,
        'action': action,
        'at': at,
        'expected_version': expectedVersion,
        if (minutes != null) 'minutes': minutes,
        if (mode != null) 'mode': mode,
      };

  factory PendingAction.fromMap(Map<String, Object?> map) => PendingAction(
        id: map['id'] as String,
        action: map['action'] as String,
        at: map['at'] as String,
        expectedVersion: (map['expected_version'] as num).toInt(),
        minutes: (map['minutes'] as num?)?.toInt(),
        mode: map['mode'] as String?,
      );
}

/// DM 端离线队列。上限 200 条；满了拒绝继续离线推进，避免无限堆积。
class ActionQueue {
  ActionQueue({this.databaseName = 'dm_queue.db', this.maxItems = 200});

  final String databaseName;
  final int maxItems;
  Database? _db;

  Future<Database> _open() async {
    final existing = _db;
    if (existing != null) {
      return existing;
    }
    final root = await getDatabasesPath();
    final db = await openDatabase(
      p.join(root, databaseName),
      version: 1,
      onCreate: (database, version) async {
        await database.execute('''
          CREATE TABLE pending_actions (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            at TEXT NOT NULL,
            expected_version INTEGER NOT NULL,
            minutes INTEGER,
            mode TEXT,
            created_at INTEGER NOT NULL
          )
        ''');
      },
    );
    _db = db;
    return db;
  }

  Future<bool> enqueue(PendingAction action) async {
    final db = await _open();
    final count = Sqflite.firstIntValue(
          await db.rawQuery('SELECT COUNT(*) FROM pending_actions'),
        ) ??
        0;
    if (count >= maxItems) {
      return false;
    }
    await db.insert(
      'pending_actions',
      {...action.toMap(), 'created_at': DateTime.now().millisecondsSinceEpoch},
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
    return true;
  }

  Future<List<PendingAction>> pending() async {
    final db = await _open();
    final rows = await db.query('pending_actions', orderBy: 'created_at ASC');
    return rows.map(PendingAction.fromMap).toList();
  }

  Future<void> removeAll(Iterable<String> ids) async {
    if (ids.isEmpty) {
      return;
    }
    final db = await _open();
    final placeholders = List.filled(ids.length, '?').join(',');
    await db.delete(
      'pending_actions',
      where: 'id IN ($placeholders)',
      whereArgs: ids.toList(),
    );
  }

  Future<void> clear() async {
    final db = await _open();
    await db.delete('pending_actions');
  }

  Future<void> close() async {
    await _db?.close();
    _db = null;
  }
}
