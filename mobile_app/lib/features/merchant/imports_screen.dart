import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../ui/widgets.dart';

/// 渠道单与快速记单。
///
/// 不抓取大众点评：只接收店长手工导出的 CSV/XLSX；每次导入都显示数据截止时间。
class ImportsScreen extends StatefulWidget {
  const ImportsScreen({super.key, required this.api});

  final ApiClient api;

  @override
  State<ImportsScreen> createState() => _ImportsScreenState();
}

class _ImportsScreenState extends State<ImportsScreen> {
  Map<String, dynamic>? _schedule;
  bool _loading = true;
  String? _loadError;

  String _selectedSessionId = '';
  int _seats = 1;
  String? _bookingRef;
  bool _bookingBusy = false;
  String? _bookingError;
  Map<String, dynamic>? _bookingResult;

  final _dataAsOf = TextEditingController();
  String? _fileName;
  List<int>? _fileBytes;
  bool _importBusy = false;
  String? _importError;
  Map<String, dynamic>? _preview;
  Map<String, dynamic>? _commit;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _dataAsOf.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _loadError = null;
    });
    try {
      final schedule = await widget.api.get('/api/app/schedule');
      if (!mounted) {
        return;
      }
      setState(() {
        _schedule = schedule;
        _loading = false;
      });
    } on ApiException catch (exc) {
      if (!mounted) {
        return;
      }
      setState(() {
        _loading = false;
        _loadError = exc.message;
      });
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() {
        _loading = false;
        _loadError = '网络异常，无法加载场次。';
      });
    }
  }

  List<Map<String, dynamic>> get _sessions {
    final sessions = asMapList(_schedule?['sessions']).where((item) {
      return sessionStatusKey(item['status']) != 'cancelled';
    }).toList();
    sessions.sort(
      (a, b) => textOf(a['start_time']).compareTo(textOf(b['start_time'])),
    );
    return sessions;
  }

  Map<String, dynamic>? get _selectedSession {
    for (final item in _sessions) {
      if (textOf(item['session_id']) == _selectedSessionId) {
        return item;
      }
    }
    return null;
  }

  Future<void> _book() async {
    if (_bookingBusy) {
      return;
    }
    final session = _selectedSession;
    if (session == null) {
      setState(() => _bookingError = '先选择要记单的场次。');
      return;
    }
    // 同一个意图复用 client_ref：网络失败后重试不会重复加座。
    _bookingRef ??= const Uuid().v4();
    setState(() {
      _bookingBusy = true;
      _bookingError = null;
      _bookingResult = null;
    });
    try {
      final result = await widget.api.post(
        '/api/app/bookings/quick',
        body: {
          'session_id': textOf(session['session_id']),
          'seats': _seats,
          'client_ref': _bookingRef,
        },
      );
      _bookingRef = null;
      final updated = asMap(result['session']);
      if (updated.isNotEmpty) {
        _replaceSession(updated);
      }
      if (!mounted) {
        return;
      }
      setState(() {
        _bookingBusy = false;
        _bookingResult = result;
      });
    } on ApiException catch (exc) {
      if (!mounted) {
        return;
      }
      var message = exc.message;
      final body = exc.body;
      if (exc.code == 'capacity_exceeded' && body != null) {
        final remaining = intOf(body['remaining_seats']);
        final total = intOf(body['total_seats']);
        message = '场次已满'
            '${remaining == null ? '' : '（剩余 $remaining 个座位'}'
            '${total == null ? '' : '，共 $total）'}；建议候补或换场。';
        _bookingRef = null;
      }
      if (exc.isNetwork) {
        message = '$message 网络恢复后可以原地重试，不会重复占座。';
      }
      setState(() {
        _bookingBusy = false;
        _bookingError = message;
      });
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() {
        _bookingBusy = false;
        _bookingError = '网络异常，可重试；同一操作不会重复占座。';
      });
    }
  }

  void _replaceSession(Map<String, dynamic> updated) {
    final schedule = _schedule;
    if (schedule == null) {
      return;
    }
    final sessions = asMapList(schedule['sessions']).map((item) {
      if (textOf(item['session_id']) == textOf(updated['session_id'])) {
        return {...item, ...updated};
      }
      return item;
    }).toList();
    _schedule = {...schedule, 'sessions': sessions};
  }

  Future<void> _pickFile() async {
    if (_importBusy) {
      return;
    }
    setState(() {
      _importError = null;
      _preview = null;
      _commit = null;
    });
    try {
      final result = await FilePicker.platform.pickFiles(
        type: FileType.custom,
        allowedExtensions: const ['csv', 'xlsx'],
        withData: true,
      );
      if (result == null || result.files.isEmpty) {
        return;
      }
      final file = result.files.single;
      var bytes = file.bytes;
      if (bytes == null && file.path != null) {
        bytes = await File(file.path!).readAsBytes();
      }
      if (bytes == null) {
        setState(() => _importError = '无法读取这个文件，请重新选择。');
        return;
      }
      if (!mounted) {
        return;
      }
      setState(() {
        _fileName = file.name;
        _fileBytes = bytes;
      });
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() => _importError = '文件选择失败，请检查系统文件权限。');
    }
  }

  Future<void> _runImport({required bool commit}) async {
    if (_importBusy) {
      return;
    }
    final name = _fileName;
    final bytes = _fileBytes;
    if (name == null || bytes == null) {
      setState(() => _importError = '先选择大众点评导出的 CSV 或 XLSX 文件。');
      return;
    }
    final fields = <String, String>{};
    final asOf = _dataAsOf.text.trim();
    if (asOf.isNotEmpty) {
      fields['data_as_of'] = asOf;
    }
    if (commit) {
      fields['confirm'] = 'true';
    }
    setState(() {
      _importBusy = true;
      _importError = null;
    });
    try {
      final result = await widget.api.upload(
        commit
            ? '/api/app/imports/dianping/commit'
            : '/api/app/imports/dianping/preview',
        filename: name,
        bytes: bytes,
        fields: fields,
      );
      if (!mounted) {
        return;
      }
      setState(() {
        _importBusy = false;
        if (commit) {
          _commit = result;
        } else {
          _preview = result;
        }
      });
      if (commit) {
        _load();
      }
    } on ApiException catch (exc) {
      if (!mounted) {
        return;
      }
      setState(() {
        _importBusy = false;
        _importError = exc.message;
      });
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() {
        _importBusy = false;
        _importError = '网络异常，导入未确认；请稍后重新预览。';
      });
    }
  }

  void _resetImport() {
    setState(() {
      _fileName = null;
      _fileBytes = null;
      _preview = null;
      _commit = null;
      _importError = null;
      _dataAsOf.clear();
    });
  }

  @override
  Widget build(BuildContext context) {
    final sessions = _sessions;
    final selected = _selectedSession;
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(16, 10, 16, 28),
        children: [
          const SectionTitle(title: '快速记单'),
          Panel(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  '内部预约会先复核容量，再写入同一本账；'
                  '写满时立即拒绝，不会在系统内超卖。',
                  style: TextStyle(
                    color: AppColors.textDim,
                    fontSize: 12,
                    height: 1.5,
                  ),
                ),
                const SizedBox(height: 14),
                if (_loading)
                  const Center(
                    child: Padding(
                      padding: EdgeInsets.all(16),
                      child: CircularProgressIndicator(),
                    ),
                  )
                else if (_loadError != null)
                  InlineError(message: _loadError!, onRetry: _load)
                else if (sessions.isEmpty)
                  const EmptyHint(
                    title: '还没有可记单的场次',
                    message: '先到排期页新增场次，再回来记录座位。',
                    icon: Icons.event_busy_outlined,
                  )
                else ...[
                  DropdownButtonFormField<String>(
                    initialValue: _selectedSessionId.isEmpty
                        ? null
                        : _selectedSessionId,
                    isExpanded: true,
                    decoration: const InputDecoration(labelText: '场次'),
                    hint: const Text('选择场次'),
                    items: [
                      for (final item in sessions)
                        DropdownMenuItem(
                          value: textOf(item['session_id']),
                          child: Text(
                            '${dateOf(item['start_time'])} '
                            '${clockOf(item['start_time'])} · '
                            '${textOf(item['script_name'], fallback: '未命名剧本')}',
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                    ],
                    onChanged: _bookingBusy
                        ? null
                        : (value) {
                            setState(() {
                              _selectedSessionId = value ?? '';
                              _bookingRef = null;
                              _bookingError = null;
                              _bookingResult = null;
                            });
                          },
                  ),
                  if (selected != null) ...[
                    const SizedBox(height: 10),
                    Wrap(
                      spacing: 8,
                      runSpacing: 6,
                      children: [
                        Pill(
                          label: '已约 ${intOf(selected['booked_seats']) ?? 0} / '
                              '${intOf(selected['total_seats'])?.toString() ?? '容量待补'}',
                          color: AppColors.cyan,
                          icon: Icons.event_seat_outlined,
                        ),
                        Pill(
                          label: assignmentText(selected),
                          color: AppColors.violet,
                        ),
                        if (intOf(selected['remaining_seats']) != null)
                          Pill(
                            label: '余 ${intOf(selected['remaining_seats'])} 位',
                            color: AppColors.mint,
                          ),
                      ],
                    ),
                  ],
                  const SizedBox(height: 14),
                  const Text(
                    '本次人数',
                    style: TextStyle(color: AppColors.textDim, fontSize: 12),
                  ),
                  const SizedBox(height: 8),
                  SegmentedButton<int>(
                    segments: const [
                      ButtonSegment(value: 1, label: Text('+1')),
                      ButtonSegment(value: 2, label: Text('+2')),
                      ButtonSegment(value: 3, label: Text('+3')),
                    ],
                    selected: {_seats},
                    onSelectionChanged: _bookingBusy
                        ? null
                        : (value) => setState(() {
                              _seats = value.first;
                              _bookingRef = null;
                            }),
                  ),
                  const SizedBox(height: 14),
                  FilledButton(
                    onPressed: _bookingBusy || selected == null ? null : _book,
                    child: _bookingBusy
                        ? const SizedBox(
                            width: 22,
                            height: 22,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : Text('记 $_seats 个座位'),
                  ),
                  if (_bookingError != null) ...[
                    const SizedBox(height: 12),
                    InlineError(message: _bookingError!),
                  ],
                  if (_bookingResult != null) ...[
                    const SizedBox(height: 12),
                    _BookingSuccess(result: _bookingResult!),
                  ],
                ],
              ],
            ),
          ),
          const SizedBox(height: 24),
          const SectionTitle(title: '大众点评订单导入'),
          Panel(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: AppColors.amber.withValues(alpha: 0.08),
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: AppColors.amber.withValues(alpha: 0.24)),
                  ),
                  child: const Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Icon(Icons.info_outline, color: AppColors.amber, size: 16),
                      SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          '不是实时同步：只有导入导出文件后，系统才看见这些座位。'
                          '两次导入之间点评端仍可能接单。不抓取、不登录商家后台。',
                          style: TextStyle(
                            color: AppColors.textDim,
                            fontSize: 12,
                            height: 1.5,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 14),
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        _fileName ?? '未选择文件',
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          color: _fileName == null
                              ? AppColors.textFaint
                              : AppColors.text,
                          fontSize: 13,
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    OutlinedButton.icon(
                      onPressed: _importBusy ? null : _pickFile,
                      icon: const Icon(Icons.attach_file, size: 18),
                      label: const Text('选择文件'),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                const Text(
                  '支持点评后台导出的 CSV / XLSX，最大 2 MB、5000 行；'
                  '只识别订单号、场次、人数、状态等字段，电话列不落库。',
                  style: TextStyle(
                    color: AppColors.textFaint,
                    fontSize: 11,
                    height: 1.5,
                  ),
                ),
                const SizedBox(height: 14),
                TextField(
                  controller: _dataAsOf,
                  enabled: !_importBusy,
                  decoration: const InputDecoration(
                    labelText: '数据截止时间（可选）',
                    hintText: '2026-10-21 18:30；不填按上传时间',
                  ),
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton(
                        onPressed: _importBusy ? null : () => _runImport(commit: false),
                        child: _importBusy && _preview == null
                            ? const SizedBox(
                                width: 20,
                                height: 20,
                                child: CircularProgressIndicator(strokeWidth: 2),
                              )
                            : const Text('预览'),
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: FilledButton(
                        onPressed: _preview == null || _importBusy
                            ? null
                            : () => _runImport(commit: true),
                        child: const Text('确认导入'),
                      ),
                    ),
                  ],
                ),
                if (_importError != null) ...[
                  const SizedBox(height: 12),
                  InlineError(message: _importError!),
                ],
                if (_commit != null) ...[
                  const SizedBox(height: 14),
                  _CommitPanel(result: _commit!, onReset: _resetImport),
                ] else if (_preview != null) ...[
                  const SizedBox(height: 14),
                  _PreviewPanel(result: _preview!),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _BookingSuccess extends StatelessWidget {
  const _BookingSuccess({required this.result});

  final Map<String, dynamic> result;

  @override
  Widget build(BuildContext context) {
    final session = asMap(result['session']);
    final warnings = asStringList(result['warnings']);
    final unchanged = result['unchanged'] == true;
    return Panel(
      borderColor: AppColors.mint.withValues(alpha: 0.3),
      padding: const EdgeInsets.all(12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.check_circle_outline,
                  color: AppColors.mint, size: 18),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  unchanged
                      ? '这次操作已经记过，没有重复加座。'
                      : '已记入 ${textOf(session['script_name'], fallback: '场次')}',
                  style: const TextStyle(
                    color: AppColors.text,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            '现在 ${intOf(session['booked_seats']) ?? 0} / '
            '${intOf(session['total_seats'])?.toString() ?? '容量待补'} 人',
            style: const TextStyle(color: AppColors.textDim, fontSize: 12),
          ),
          for (final warning in warnings)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                warning,
                style: const TextStyle(
                  color: AppColors.amber,
                  fontSize: 11,
                  height: 1.45,
                ),
              ),
            ),
        ],
      ),
    );
  }
}

class _PreviewPanel extends StatelessWidget {
  const _PreviewPanel({required this.result});

  final Map<String, dynamic> result;

  @override
  Widget build(BuildContext context) {
    final total = intOf(result['total_rows']) ?? 0;
    final valid = intOf(result['valid_rows']) ?? 0;
    final errors = asMapList(result['errors']);
    final warnings = asStringList(result['warnings']);
    final over = asMapList(result['over_capacity']);
    final unmatched = asMapList(result['unmatched']);
    final changes = asMapList(result['session_changes']);
    final rows = asMapList(result['preview']);
    final dataAsOf = textOf(result['data_as_of']);
    final stale = result['stale'] == true;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            const Pill(label: '预览', color: AppColors.cyan),
            const SizedBox(width: 8),
            Text(
              '共 $total 行 · 可导入 $valid 行'
              '${errors.isEmpty ? '' : ' · 错误 ${errors.length}'}',
              style: const TextStyle(color: AppColors.textDim, fontSize: 12),
            ),
          ],
        ),
        if (dataAsOf.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(
            '数据截止 $dataAsOf${stale ? '（已过期，建议重新导出）' : ''}',
            style: TextStyle(
              color: stale ? AppColors.danger : AppColors.textFaint,
              fontSize: 12,
              fontWeight: stale ? FontWeight.w600 : FontWeight.normal,
            ),
          ),
        ],
        for (final warning in warnings)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(
              warning,
              style: const TextStyle(
                color: AppColors.amber,
                fontSize: 12,
                height: 1.45,
              ),
            ),
          ),
        if (over.isNotEmpty) ...[
          const SizedBox(height: 12),
          const Text(
            '以下场次会超出容量，整组不会导入：',
            style: TextStyle(
              color: AppColors.danger,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          for (final item in over)
            _Line(
              text: '${textOf(item['script_name'], fallback: '场次')} '
                  '当前 ${intOf(item['current_booked']) ?? 0} 人，'
                  '净增 ${intOf(item['net_delta']) ?? 0}，'
                  '预计 ${intOf(item['projected_booked']) ?? 0} / '
                  '${intOf(item['total_seats'])?.toString() ?? '未知'}',
              color: AppColors.danger,
            ),
        ],
        if (unmatched.isNotEmpty) ...[
          const SizedBox(height: 12),
          Text(
            '未匹配场次 ${unmatched.length} 行（不会自动新建场次）：',
            style: const TextStyle(
              color: AppColors.amber,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          for (final item in unmatched.take(8))
            _Line(
              text: '第 ${intOf(item['row_number']) ?? '?'} 行 · '
                  '${textOf(item['script_name'], fallback: '未知剧本')} · '
                  '${textOf(item['start_time'], fallback: '未知时间')}',
              color: AppColors.textDim,
            ),
        ],
        if (changes.isNotEmpty) ...[
          const SizedBox(height: 12),
          const Text(
            '按场次汇总',
            style: TextStyle(
              color: AppColors.textDim,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          for (final item in changes.take(10))
            _Line(
              text: '${textOf(item['script_name'], fallback: '场次')}：'
                  '${intOf(item['net_delta']) != null && (intOf(item['net_delta']) ?? 0) >= 0 ? '+' : ''}'
                  '${intOf(item['net_delta']) ?? 0} 人',
              color: AppColors.textDim,
            ),
        ],
        if (rows.isNotEmpty) ...[
          const SizedBox(height: 12),
          const Text(
            '前 10 行',
            style: TextStyle(
              color: AppColors.textDim,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          for (final row in rows)
            _Line(
              text: '${textOf(row['external_id'], fallback: '无订单号')} · '
                  '${textOf(row['script_name'], fallback: '未匹配')} · '
                  '${intOf(row['seats']) ?? 0} 人 · '
                  '${textOf(row['action'], fallback: textOf(row['error'], fallback: '-'))}',
              color: textOf(row['error']).isEmpty
                  ? AppColors.textDim
                  : AppColors.danger,
            ),
        ],
        if (errors.isNotEmpty) ...[
          const SizedBox(height: 12),
          Text(
            '错误 ${errors.length} 行',
            style: const TextStyle(
              color: AppColors.danger,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          for (final item in errors.take(8))
            _Line(
              text: '第 ${intOf(item['row_number']) ?? '?'} 行：'
                  '${textOf(item['error'], fallback: '无法解析')}',
              color: AppColors.danger,
            ),
        ],
      ],
    );
  }
}

class _CommitPanel extends StatelessWidget {
  const _CommitPanel({required this.result, required this.onReset});

  final Map<String, dynamic> result;
  final VoidCallback onReset;

  @override
  Widget build(BuildContext context) {
    final warnings = asStringList(result['warnings']);
    final changes = asMapList(result['session_changes']);
    return Panel(
      borderColor: AppColors.mint.withValues(alpha: 0.3),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Row(
            children: [
              Icon(Icons.task_alt, color: AppColors.mint, size: 20),
              SizedBox(width: 8),
              Text(
                '导入完成',
                style: TextStyle(
                  color: AppColors.text,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Text(
            '新增 ${intOf(result['imported']) ?? 0} · '
            '更新 ${intOf(result['updated']) ?? 0} · '
            '取消 ${intOf(result['cancelled']) ?? 0} · '
            '未变 ${intOf(result['unchanged']) ?? 0} · '
            '拒绝 ${intOf(result['rejected']) ?? 0}',
            style: const TextStyle(
              color: AppColors.textDim,
              fontSize: 12,
              height: 1.6,
            ),
          ),
          if (changes.isNotEmpty) ...[
            const SizedBox(height: 8),
            for (final item in changes.take(10))
              _Line(
                text: '${textOf(item['script_name'], fallback: '场次')}：'
                    '${intOf(item['net_delta']) ?? 0} 人',
                color: AppColors.textDim,
              ),
          ],
          for (final warning in warnings)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                warning,
                style: const TextStyle(
                  color: AppColors.amber,
                  fontSize: 11,
                  height: 1.45,
                ),
              ),
            ),
          const SizedBox(height: 12),
          OutlinedButton(
            onPressed: onReset,
            child: const Text('再导入一个文件'),
          ),
        ],
      ),
    );
  }
}

class _Line extends StatelessWidget {
  const _Line({required this.text, required this.color});

  final String text;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Text(
        text,
        style: TextStyle(color: color, fontSize: 12, height: 1.45),
      ),
    );
  }
}
