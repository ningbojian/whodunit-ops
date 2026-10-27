import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

/// 统一的后端错误。业务失败不会抛未捕获异常，API 层统一转成这个类型。
class ApiException implements Exception {
  ApiException(this.code, this.message, {this.status = 0, this.body});

  final String code;
  final String message;
  final int status;
  final Map<String, dynamic>? body;

  bool get isNetwork => status == 0;
  bool get isStaleState => code == 'stale_state';

  @override
  String toString() => '$code: $message';
}

/// 极薄的 HTTP 封装：Bearer、超时、JSON 信封和 multipart 上传。
///
/// 不在这里做业务判断；会话、排期、场控字段全部来自服务端。
class ApiClient {
  ApiClient({
    required this.baseUrl,
    http.Client? httpClient,
  }) : _http = httpClient ?? http.Client();

  final String baseUrl;
  final http.Client _http;
  String? token;

  static const int _timeoutSeconds = 30;

  Uri _uri(String path, [Map<String, String>? query]) {
    final root = baseUrl.endsWith('/')
        ? baseUrl.substring(0, baseUrl.length - 1)
        : baseUrl;
    final uri = Uri.parse('$root$path');
    if (query == null || query.isEmpty) {
      return uri;
    }
    return uri.replace(queryParameters: {...uri.queryParameters, ...query});
  }

  Map<String, String> _headers({
    bool json = true,
    Map<String, String>? extra,
  }) {
    final headers = <String, String>{'Accept': 'application/json'};
    if (json) {
      headers['Content-Type'] = 'application/json';
    }
    final current = token;
    if (current != null && current.isNotEmpty) {
      headers['Authorization'] = 'Bearer $current';
    }
    if (extra != null) {
      headers.addAll(extra);
    }
    return headers;
  }

  Future<Map<String, dynamic>> get(
    String path, {
    Map<String, String>? query,
    Map<String, String>? headers,
  }) async {
    final response = await _send(
      () => _http.get(
        _uri(path, query),
        headers: _headers(json: false, extra: headers),
      ),
    );
    return _decode(response);
  }

  Future<Map<String, dynamic>> post(String path, {Object? body}) async {
    final response = await _send(
      () => _http.post(
        _uri(path),
        headers: _headers(),
        body: body == null ? null : jsonEncode(body),
      ),
    );
    return _decode(response);
  }

  Future<Map<String, dynamic>> patch(String path, {Object? body}) async {
    final response = await _send(
      () => _http.patch(
        _uri(path),
        headers: _headers(),
        body: body == null ? null : jsonEncode(body),
      ),
    );
    return _decode(response);
  }

  Future<Map<String, dynamic>> delete(String path) async {
    final response = await _send(
      () => _http.delete(_uri(path), headers: _headers(json: false)),
    );
    return _decode(response);
  }

  Future<Map<String, dynamic>> upload(
    String path, {
    required String filename,
    required List<int> bytes,
    Map<String, String> fields = const {},
  }) async {
    final request = http.MultipartRequest('POST', _uri(path));
    request.headers.addAll(_headers(json: false));
    request.fields.addAll(fields);
    request.files.add(
      http.MultipartFile.fromBytes('file', bytes, filename: filename),
    );
    final streamed = await _send(() async {
      final response = await _http.send(request).timeout(
            const Duration(seconds: 60),
          );
      return http.Response.fromStream(response);
    });
    return _decode(streamed);
  }

  Future<http.Response> _send(Future<http.Response> Function() action) async {
    try {
      return await action().timeout(const Duration(seconds: _timeoutSeconds));
    } on TimeoutException {
      throw ApiException('network_timeout', '网络超时，请稍后重试。');
    } on http.ClientException {
      throw ApiException('network_error', '网络连接失败。');
    }
  }

  Map<String, dynamic> _decode(http.Response response) {
    Map<String, dynamic> payload = const {};
    if (response.body.isNotEmpty) {
      try {
        final decoded = jsonDecode(response.body);
        if (decoded is Map<String, dynamic>) {
          payload = decoded;
        }
      } on FormatException {
        throw ApiException(
          'bad_response',
          '服务端返回了无法解析的内容。',
          status: response.statusCode,
        );
      }
    }

    final ok = response.statusCode >= 200 &&
        response.statusCode < 300 &&
        payload['ok'] == true;
    if (!ok) {
      final code = payload['code']?.toString() ??
          (response.statusCode == 401 ? 'unauthorized' : 'request_failed');
      final message =
          payload['error']?.toString() ?? '请求失败（${response.statusCode}）。';
      throw ApiException(
        code,
        message,
        status: response.statusCode,
        body: payload,
      );
    }
    return payload;
  }

  void close() => _http.close();
}
