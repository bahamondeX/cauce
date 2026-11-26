<?php
declare(strict_types=1);

$mode = $_GET['mode'] ?? 'request';
header('Content-Type: application/json');
if ($mode === 'redirect') {
    header('Location: /next', true, 307);
    exit;
}
if ($mode === 'cookies') {
    header('Set-Cookie: a=one; Path=/', false);
    header('Set-Cookie: b=two; Path=/', false);
}
if ($mode === 'error') {
    http_response_code(500);
}
if ($mode === 'large' || $mode === 'slow') {
    header('Content-Type: application/octet-stream');
    if ($mode === 'slow') {
        while (ob_get_level() > 0) {
            ob_end_flush();
        }
        echo str_repeat('x', 16384);
        flush();
        usleep(500000);
        echo str_repeat('x', 16384);
        exit;
    }
    header('Content-Length: 1048576');
    for ($i = 0; $i < 64; $i++) {
        echo str_repeat('x', 16384);
    }
    exit;
}
if ($mode === 'cache') {
    header('Cache-Control: public, max-age=60');
}
$body = file_get_contents('php://input');
$upload = $_FILES['fixture'] ?? null;
echo json_encode([
    'method' => $_SERVER['REQUEST_METHOD'],
    'script' => $_SERVER['SCRIPT_FILENAME'],
    'script_name' => $_SERVER['SCRIPT_NAME'],
    'root' => $_SERVER['DOCUMENT_ROOT'],
    'server' => $_SERVER['SERVER_NAME'],
    'remote' => $_SERVER['REMOTE_ADDR'],
    'query' => $_SERVER['QUERY_STRING'],
    'bytes' => strlen($body),
    'sha256' => hash('sha256', $body),
    'proxy' => ($_SERVER['HTTP_PROXY'] ?? '') ?: null,
    'upload' => $upload === null ? null : [
        'error' => $upload['error'],
        'size' => $upload['size'],
        'sha256' => hash_file('sha256', $upload['tmp_name']),
    ],
], JSON_THROW_ON_ERROR);
