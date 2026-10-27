import 'dart:async';

import 'package:flutter/material.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../ui/widgets.dart';

/// 商家端场控直播：只读。没有推进、暂停或结束按钮。
class RunDetailScreen extends StatefulWidget {
  const RunDetailScreen({
    super.key,
    required this.api,
    required this.runId,
    this.initialTitle,
  });

  final ApiClient api;
  final String runId;
  final String? initialTitle;

  @override
  State<RunDetailScreen> createState() => _RunDetailScreenState();
}

class _RunDetailScreenState extends State<RunDetailScreen> {
  Map<String, dynamic>? _view;
  bool _loading = true;
  String? _error;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _load();
    _timer = Timer.periodic(const Duration(seconds: 10), (_) => _load(silent: true));
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _load({bool silent = false}) async {
    if (!silent) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }
    try {
      final result = await widget.api.get('/api/app/runs/${widget.runId}');
      if (!mounted) {
        return;
      }
      setState(() {
        _view = result;
        _loading = false;
        _error = null;
      });
    } on ApiException catch (exc) {
      if (!mounted) {
        return;
      }
      setState(() {
        _loading = false;
        if (!silent) {
          _error = exc.message;
        }
      });
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() {
        _loading = false;
        if (!silent) {
          _error = '网络异常，无法刷新场控状态。';
        }
      });
    }
  }

  Future<void> _issueCode() async {
    try {
      final result = await widget.api.post('/api/app/runs/${widget.runId}/code');
      final code = textOf(result['code']);
      if (!mounted) {
        return;
      }
      if (code.isEmpty) {
        return;
      }
      await showDialog<void>(
        context: context,
        builder: (context) => AlertDialog(
          backgroundColor: AppColors.bg2,
          title: const Text('新的场控码'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SelectableText(
                code,
                style: const TextStyle(
                  fontSize: 30,
                  letterSpacing: 8,
                  fontWeight: FontWeight.w800,
                  color: AppColors.cyan,
                ),
              ),
              const SizedBox(height: 14),
              Text(
                '有效期至 ${textOf(result['code_expires_at'], fallback: '稍后')}。'
                '只显示这一次，旧令牌已失效。',
                style: const TextStyle(color: AppColors.textDim, height: 1.5),
              ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('关闭'),
            ),
          ],
        ),
      );
    } on ApiException catch (exc) {
      _snack(exc.message);
    } catch (_) {
      _snack('网络异常，请稍后重试。');
    }
  }

  Future<void> _revoke() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: AppColors.bg2,
        title: const Text('撤销 DM 令牌'),
        content: const Text('撤销后，当前 DM 设备会立即退出；场控本身不会结束。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('撤销'),
          ),
        ],
      ),
    );
    if (confirmed != true) {
      return;
    }
    try {
      await widget.api.post('/api/app/runs/${widget.runId}/revoke');
      _snack('已撤销当前 DM 令牌。');
      _load();
    } on ApiException catch (exc) {
      _snack(exc.message);
    } catch (_) {
      _snack('网络异常，请稍后重试。');
    }
  }

  void _snack(String message) {
    if (!mounted) {
      return;
    }
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    final view = _view ?? const <String, dynamic>{};
    final run = asMap(view['run']);
    final session = asMap(view['session']);
    final currentStage = asMap(view['current_stage']);
    final stages = asMapList(view['stages']);
    final warnings = asStringList(view['warnings']);
    final status = textOf(run['status']);
    final title = textOf(
      session['script_name'],
      fallback: widget.initialTitle ?? '场控详情',
    );

    return Scaffold(
      appBar: AppBar(
        title: Text(title),
        actions: [
          IconButton(
            onPressed: () => _load(),
            icon: const Icon(Icons.refresh),
            tooltip: '刷新',
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () => _load(),
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
          children: [
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: AppColors.bg2.withValues(alpha: 0.6),
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: AppColors.line),
              ),
              child: const Row(
                children: [
                  Icon(Icons.visibility_outlined,
                      color: AppColors.textFaint, size: 16),
                  SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      '商家端只读：可以查看、生成新码或撤销令牌，但不能推进、暂停或结束场控。',
                      style: TextStyle(
                        color: AppColors.textFaint,
                        fontSize: 12,
                        height: 1.5,
                      ),
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 14),
            if (_loading && _view == null)
              const Center(
                child: Padding(
                  padding: EdgeInsets.all(32),
                  child: CircularProgressIndicator(),
                ),
              )
            else if (_error != null && _view == null)
              InlineError(message: _error!, onRetry: _load)
            else ...[
              _StatusPanel(
                run: run,
                session: session,
                currentStage: currentStage,
              ),
              if (warnings.isNotEmpty) ...[
                const SizedBox(height: 12),
                for (final item in warnings)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: _WarningLine(text: item),
                  ),
              ],
              const SizedBox(height: 18),
              const SectionTitle(title: '阶段进度'),
              if (stages.isEmpty)
                const EmptyHint(
                  title: '暂无阶段模板',
                  message: '该剧本还没有可用的阶段模板。',
                )
              else
                for (final stage in stages)
                  _StageRow(stage: stage),
              const SizedBox(height: 20),
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: status == 'completed' || status == 'abandoned'
                          ? null
                          : _issueCode,
                      icon: const Icon(Icons.key_outlined, size: 18),
                      label: const Text('生成新场控码'),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: status == 'completed' || status == 'abandoned'
                          ? null
                          : _revoke,
                      icon: const Icon(Icons.block_outlined, size: 18),
                      label: const Text('撤销令牌'),
                    ),
                  ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _StatusPanel extends StatelessWidget {
  const _StatusPanel({
    required this.run,
    required this.session,
    required this.currentStage,
  });

  final Map<String, dynamic> run;
  final Map<String, dynamic> session;
  final Map<String, dynamic> currentStage;

  @override
  Widget build(BuildContext context) {
    final status = textOf(run['status']);
    final color = runStatusColor(status);
    final remaining = intOf(run['remaining_seconds']) ?? 0;
    final overtime = intOf(run['overtime_seconds']) ?? 0;
    final seq = intOf(run['current_seq']);
    final stageName = textOf(currentStage['name'], fallback: '—');
    return Panel(
      borderColor: color.withValues(alpha: 0.34),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Pill(label: runStatusLabel(status), color: color),
              const SizedBox(width: 8),
              Pill(
                label: textOf(run['mode']) == 'hardware' ? '硬件模式' : '软件模式',
                color: AppColors.violet,
                icon: Icons.lightbulb_outline,
              ),
              if (run['hardware_degraded'] == true) ...[
                const SizedBox(width: 8),
                const Pill(label: '硬件已降级', color: AppColors.amber),
              ],
            ],
          ),
          const SizedBox(height: 16),
          Text(
            textOf(session['script_name'], fallback: '场次'),
            style: const TextStyle(
              color: AppColors.text,
              fontSize: 20,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            '${textOf(session['start_time'])} · '
            '${textOf(run['room_name'], fallback: '房间未知')} · '
            '${textOf(run['dm_name'], fallback: '临时 DM')}',
            style: const TextStyle(color: AppColors.textDim, fontSize: 13),
          ),
          const SizedBox(height: 18),
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Text(
                durationOf(remaining),
                style: TextStyle(
                  fontSize: 40,
                  fontWeight: FontWeight.w800,
                  letterSpacing: 2,
                  color: overtime > 0 ? AppColors.danger : AppColors.text,
                  fontFeatures: const [FontFeature.tabularFigures()],
                ),
              ),
              const SizedBox(width: 12),
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Text(
                  status == 'paused'
                      ? '已暂停 · 剩余'
                      : overtime > 0
                          ? '已超时 +${durationOf(overtime)}'
                          : '剩余',
                  style: TextStyle(
                    color: overtime > 0 ? AppColors.danger : AppColors.textDim,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            seq == null ? '尚未开始' : '第 $seq 幕 · $stageName',
            style: const TextStyle(color: AppColors.textDim, fontSize: 13),
          ),
        ],
      ),
    );
  }
}

class _StageRow extends StatelessWidget {
  const _StageRow({required this.stage});

  final Map<String, dynamic> stage;

  @override
  Widget build(BuildContext context) {
    final state = textOf(stage['state']);
    final color = switch (state) {
      'completed' => AppColors.mint,
      'current' => AppColors.cyan,
      _ => AppColors.textFaint,
    };
    final label = switch (state) {
      'completed' => '已完成',
      'current' => '当前',
      _ => '未开始',
    };
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Panel(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        borderColor: color.withValues(alpha: state == 'current' ? 0.5 : 0.18),
        child: Row(
          children: [
            Text(
              '${intOf(stage['seq']) ?? '-'}',
              style: TextStyle(
                color: color,
                fontWeight: FontWeight.w800,
                fontSize: 16,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                textOf(stage['name'], fallback: '未命名阶段'),
                style: TextStyle(
                  color: state == 'upcoming' ? AppColors.textDim : AppColors.text,
                  fontWeight:
                      state == 'current' ? FontWeight.w700 : FontWeight.w500,
                ),
              ),
            ),
            Text(
              '${intOf(stage['duration_minutes']) ?? 0} 分钟',
              style: const TextStyle(color: AppColors.textFaint, fontSize: 12),
            ),
            const SizedBox(width: 10),
            Pill(label: label, color: color),
          ],
        ),
      ),
    );
  }
}

class _WarningLine extends StatelessWidget {
  const _WarningLine({required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: AppColors.amber.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: AppColors.amber.withValues(alpha: 0.24)),
      ),
      child: Row(
        children: [
          const Icon(Icons.info_outline, color: AppColors.amber, size: 16),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              text,
              style: const TextStyle(
                color: AppColors.textDim,
                fontSize: 12,
                height: 1.5,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
