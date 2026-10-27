import 'package:flutter/material.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../ui/choice_button.dart';
import '../../ui/widgets.dart';
import 'choice_actions.dart';

class _Message {
  _Message({
    required this.text,
    required this.user,
    this.toolCount = 0,
    this.failed = false,
    this.choices = const [],
  });

  final String text;
  final bool user;
  final int toolCount;
  final bool failed;
  final List<Map<String, dynamic>> choices;
}

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key, required this.api});

  final ApiClient api;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final _input = TextEditingController();
  final _scroll = ScrollController();
  final List<_Message> _messages = [
    _Message(
      user: false,
      text: '我是门店运营助手。直接告诉我要记什么、改什么或查什么。\n'
          '例如“明晚八点开《雾隐山庄》”，信息不完整也没关系，我会先记草稿，'
          '只问最关键的下一项。',
    ),
  ];
  bool _busy = false;
  bool _choicePending = false;

  static const _prompts = [
    '明晚八点开《雾隐山庄》，6 人',
    '今天有几个场次，房间和 DM 够用吗',
    '给明晚的场次写一条朋友圈文案',
    '现在有哪场场控在进行',
  ];

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  /// 按钮动作：quick_reply 重发文本；其余直连业务 API，回执以系统气泡展示。
  Future<void> _onChoiceButton(Map<String, dynamic> button) async {
    if (_busy || _choicePending) {
      return;
    }
    final reply = quickReplyText(button);
    if (reply != null) {
      await _send(reply);
      return;
    }
    setState(() => _choicePending = true);
    try {
      final result = await runChoiceAction(widget.api, button);
      if (!mounted) {
        return;
      }
      setState(() {
        _messages.add(
          _Message(user: false, text: summarizeChoiceResult(result)),
        );
      });
    } on ApiException catch (exc) {
      if (!mounted) {
        return;
      }
      setState(() {
        _messages.add(
          _Message(
            user: false,
            failed: true,
            text: exc.code == 'unauthorized' || exc.code == 'token_expired'
                ? '登录已失效，请到「我的」退出后重新登录。'
                : exc.message,
          ),
        );
      });
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() {
        _messages.add(
          _Message(user: false, failed: true, text: '网络异常，请稍后重试。'),
        );
      });
    } finally {
      if (mounted) {
        setState(() => _choicePending = false);
        _scrollToEnd();
      }
    }
  }

  Future<void> _send([String? preset]) async {
    if (_busy) {
      return;
    }
    final text = (preset ?? _input.text).trim();
    if (text.isEmpty) {
      return;
    }
    setState(() {
      _messages.add(_Message(user: true, text: text));
      _input.clear();
      _busy = true;
    });
    _scrollToEnd();
    try {
      final result = await widget.api.post('/api/app/chat', body: {'message': text});
      final answer = textOf(result['answer'], fallback: '（没有返回内容）');
      final trace = result['trace'];
      final toolCount = trace is List ? trace.length : 0;
      final rawChoices = result['choices'];
      final choices = <Map<String, dynamic>>[];
      if (rawChoices is List) {
        for (final item in rawChoices) {
          if (item is Map) {
            choices.add(Map<String, dynamic>.from(item));
          }
        }
      }
      if (!mounted) {
        return;
      }
      setState(() {
        _messages.add(
          _Message(
            text: answer,
            user: false,
            toolCount: toolCount,
            choices: choices,
          ),
        );
      });
    } on ApiException catch (exc) {
      if (!mounted) {
        return;
      }
      setState(() {
        _messages.add(
          _Message(
            user: false,
            failed: true,
            text: exc.code == 'unauthorized' || exc.code == 'token_expired'
                ? '登录已失效，请到「我的」退出后重新登录。'
                : exc.message,
          ),
        );
      });
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() {
        _messages.add(
          _Message(user: false, failed: true, text: '网络异常，请稍后重试。'),
        );
      });
    } finally {
      if (mounted) {
        setState(() => _busy = false);
        _scrollToEnd();
      }
    }
  }

  void _scrollToEnd() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) {
        return;
      }
      _scroll.animateTo(
        _scroll.position.maxScrollExtent + 120,
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOut,
      );
    });
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Expanded(
          child: ListView.builder(
            controller: _scroll,
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
            itemCount: _messages.length + (_busy ? 1 : 0),
            itemBuilder: (context, index) {
              if (index >= _messages.length) {
                return const Padding(
                  padding: EdgeInsets.symmetric(vertical: 14),
                  child: Row(
                    children: [
                      SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      ),
                      SizedBox(width: 10),
                      Text(
                        '正在处理…',
                        style: TextStyle(
                          color: AppColors.textFaint,
                          fontSize: 12,
                        ),
                      ),
                    ],
                  ),
                );
              }
              return _Bubble(
                message: _messages[index],
                choicePending: _choicePending,
                onChoiceButton: _onChoiceButton,
              );
            },
          ),
        ),
        if (_messages.length <= 1)
          SizedBox(
            height: 46,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 16),
              children: [
                for (final prompt in _prompts)
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ActionChip(
                      label: Text(
                        prompt,
                        style: const TextStyle(
                          color: AppColors.textDim,
                          fontSize: 12,
                        ),
                      ),
                      backgroundColor: AppColors.bg2.withValues(alpha: 0.7),
                      side: const BorderSide(color: AppColors.line),
                      onPressed: _busy ? null : () => _send(prompt),
                    ),
                  ),
              ],
            ),
          ),
        SafeArea(
          top: false,
          child: Container(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 10),
            decoration: const BoxDecoration(
              color: AppColors.bg1,
              border: Border(top: BorderSide(color: AppColors.line)),
            ),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Expanded(
                  child: TextField(
                    controller: _input,
                    enabled: !_busy,
                    minLines: 1,
                    maxLines: 4,
                    textInputAction: TextInputAction.newline,
                    style: const TextStyle(color: AppColors.text, fontSize: 15),
                    decoration: const InputDecoration(
                      hintText: '说说要记的场次、要查的状态…',
                      isDense: true,
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                SizedBox(
                  width: 52,
                  height: 52,
                  child: IconButton.filled(
                    onPressed: _busy ? null : () => _send(),
                    icon: const Icon(Icons.arrow_upward),
                    tooltip: '发送',
                  ),
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class _Bubble extends StatelessWidget {
  const _Bubble({
    required this.message,
    this.choicePending = false,
    this.onChoiceButton,
  });

  final _Message message;
  final bool choicePending;
  final Future<void> Function(Map<String, dynamic> button)? onChoiceButton;

  @override
  Widget build(BuildContext context) {
    final user = message.user;
    final color = user
        ? AppColors.cyan.withValues(alpha: 0.14)
        : AppColors.bg2.withValues(alpha: 0.72);
    final border = user
        ? AppColors.cyan.withValues(alpha: 0.32)
        : AppColors.line;
    return Align(
      alignment: user ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.all(14),
        constraints: BoxConstraints(
          maxWidth: MediaQuery.of(context).size.width * 0.84,
        ),
        decoration: BoxDecoration(
          color: color,
          borderRadius: BorderRadius.only(
            topLeft: const Radius.circular(16),
            topRight: const Radius.circular(16),
            bottomLeft: Radius.circular(user ? 16 : 4),
            bottomRight: Radius.circular(user ? 4 : 16),
          ),
          border: Border.all(
            color: message.failed ? AppColors.danger.withValues(alpha: 0.5) : border,
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SelectableText(
              message.text,
              style: TextStyle(
                color: message.failed ? AppColors.danger : AppColors.text,
                fontSize: 14,
                height: 1.6,
              ),
            ),
            if (message.toolCount > 0) ...[
              const SizedBox(height: 8),
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.build_circle_outlined,
                      size: 13, color: AppColors.textFaint),
                  const SizedBox(width: 4),
                  Text(
                    '已调用 ${message.toolCount} 次业务工具',
                    style: const TextStyle(
                      color: AppColors.textFaint,
                      fontSize: 11,
                    ),
                  ),
                ],
              ),
            ],
            if (message.choices.isNotEmpty) ...[
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                runSpacing: 6,
                children: [
                  for (final group in message.choices)
                    for (final raw in (group['buttons'] as List? ?? const []))
                      if (raw is Map)
                        ChoiceButton(
                          label: textOf(
                            raw['label'],
                            fallback: textOf(raw['action'], fallback: '操作'),
                          ),
                          pending: choicePending,
                          onPressed: onChoiceButton == null
                              ? null
                              : () => onChoiceButton!(
                                    Map<String, dynamic>.from(raw),
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
