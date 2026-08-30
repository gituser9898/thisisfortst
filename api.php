<?php
/**
 * Arash Leaderboard API — single file, single table, MySQL-backed.
 *
 * Actions:
 *   GET  ?action=session          -> { ok, token }              call once when a run starts
 *   GET  ?action=top&limit=3      -> { ok, top: [...] }          (default GET action)
 *   POST { action: "request_otp", phone }
 *        -> { ok }                                               sends a 6-digit SMS code
 *   POST { action: "verify_otp", phone, code }
 *        -> { ok, identity_token, username }                     registers on first success
 *   POST { action: "submit", identity_token, score, token, sig }
 *        -> { ok, updated, rank, best, top }
 *
 * See README.md / README.fa.md for the full security write-up. Short
 * version: a browser game can't be made truly tamper-proof (the client
 * always computes and sends its own score), so this doesn't try to be
 * unbreakable — it makes casual/naive cheating fail, and it never trusts a
 * submission blindly: a score can only be submitted under an identity that
 * proved ownership of its phone number via OTP, tied to a server-issued
 * one-time run token, checked against a plausibility bound derived from
 * the game's real max scoring rate, and rate-limited. Every attempt
 * (accepted or not) is logged for review, since real prizes ride on this
 * data.
 *
 * Everything lives in one database table (see schema.sql) — a "type"
 * column tells each row what kind of thing it is.
 */

// ── Config — edit these ──────────────────────────────────────────────────
define('DB_HOST', getenv('DB_HOST') ?: '127.0.0.1');
define('DB_NAME', getenv('DB_NAME') ?: 'leaderboard');
define('DB_USER', getenv('DB_USER') ?: 'leaderboard');
define('DB_PASS', getenv('DB_PASS') ?: 'change-me');

// Must be the exact same string as SCORE_SIGN_KEY in index.html. Generate
// your own long random value — see README "Threat model" before relying on
// this for anything (it is NOT a real secret; it ships in client JS).
define('SIGN_KEY', getenv('SIGN_KEY') ?: 'change-this-to-a-long-random-string');

define('ALLOWED_ORIGIN', '*');          // lock to your game's domain in production
define('MIN_PHONE_LEN', 4);
define('MAX_PHONE_LEN', 20);
define('MAX_SCORE', 100000000);
define('TOP_LIMIT_MAX', 100);

// Plausibility bound. Two sources of score, both derived from index.html:
//
//   Distance: BG_MAX(1100) * SCORE_BOOST(1.2) * KM_PER_PX(1/4000)
//             * SCORE_UNIT(1000)                            = 330 pts/sec
//   Coins:    a wave every COIN_EVERY_LO(1.5)s at most, up to
//             COIN_RUN_HI(9) coins, each worth COIN_VALUE(100), and
//             assuming every single one is collected
//             = 9/1.5 * 100                                 = 600 pts/sec
//   Combined                                                = 930 pts/sec
//
// 1400 leaves roughly a 1.5x safety margin so a genuinely exceptional run
// is never rejected. Recalculate this if you change any of those
// constants -- an earlier 330 (distance only) would have rejected any
// coin-heavy run outright.
define('MAX_SCORE_PER_SEC', 1400);
define('MAX_SCORE_BONUS', 5000);        // slack for the first seconds of a run
define('MIN_RUN_SECONDS', 2);           // no legitimate run finishes faster than this
define('SESSION_MAX_AGE', 3600);        // a run token older than this is refused outright

define('RATE_LIMIT_WINDOW_SEC', 10);
define('RATE_LIMIT_MAX', 5);

// OTP settings
define('OTP_LENGTH', 6);
define('OTP_EXPIRE_SECONDS', 120);      // a code is valid for 2 minutes
define('OTP_MAX_ATTEMPTS', 5);          // wrong guesses allowed before a fresh code is required
define('OTP_REQUEST_WINDOW_SEC', 600);
define('OTP_REQUEST_MAX', 3);           // at most 3 OTP requests per phone per 10 minutes

// Legendary Persian names, combined pairwise (+ a random number) to build
// usernames like "ArashSiavash42". Edit this list freely — add or remove
// as many names as you like.
const NAME_POOL = [
    'Arash', 'Siavash', 'Rostam', 'Sohrab', 'Kaveh', 'Bahram', 'Jamshid',
    'Kourosh', 'Dariush', 'Esfandiar', 'Bijan', 'Giv', 'Faramarz', 'Manuchehr',
];

// ── DB connection ────────────────────────────────────────────────────────
function db(): PDO {
    static $pdo = null;
    if ($pdo === null) {
        $dsn = 'mysql:host=' . DB_HOST . ';dbname=' . DB_NAME . ';charset=utf8mb4';
        $pdo = new PDO($dsn, DB_USER, DB_PASS, [
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        ]);
    }
    return $pdo;
}

// ── Small helpers ────────────────────────────────────────────────────────
function respond($data, int $status = 200): void {
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($data);
    exit;
}

function cors(): void {
    header('Access-Control-Allow-Origin: ' . ALLOWED_ORIGIN);
    header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
    header('Access-Control-Allow-Headers: Content-Type');
    if (($_SERVER['REQUEST_METHOD'] ?? '') === 'OPTIONS') { http_response_code(204); exit; }
}

// Converts Persian and Arabic-Indic digits to plain ASCII digits, so a
// phone number typed on a Persian keyboard (۰۹۱۲...) still validates and
// matches correctly. Anything already ASCII passes through unchanged.
function to_ascii_digits(string $s): string {
    $persian = ['۰','۱','۲','۳','۴','۵','۶','۷','۸','۹'];
    $arabic  = ['٠','١','٢','٣','٤','٥','٦','٧','٨','٩'];
    $ascii   = ['0','1','2','3','4','5','6','7','8','9'];
    return str_replace($arabic, $ascii, str_replace($persian, $ascii, $s));
}

function valid_phone($v): bool {
    return is_string($v) && strlen($v) >= MIN_PHONE_LEN && strlen($v) <= MAX_PHONE_LEN
        && preg_match('/^[0-9+\-\s]+$/', $v);
}

function valid_score($v): bool {
    return is_numeric($v) && $v >= 0 && $v <= MAX_SCORE;
}

function client_ip(): string {
    return $_SERVER['HTTP_X_FORWARDED_FOR'] ?? $_SERVER['REMOTE_ADDR'] ?? 'unknown';
}

// ── Log rows (type='log') — audit trail + the basis for rate limiting ────
function log_attempt(string $phone, int $score, string $ip, bool $accepted, string $reason = ''): void {
    $stmt = db()->prepare('INSERT INTO board (type, phone, k2, score, num, ip, created_at) VALUES (?,?,?,?,?,?,?)');
    $stmt->execute(['log', $phone, $reason, $score, $accepted ? 1 : 0, $ip, time()]);
}

function rate_limited(string $ip, string $phone): bool {
    $stmt = db()->prepare("SELECT COUNT(*) c FROM board WHERE type='log' AND (ip = ? OR phone = ?) AND created_at > ?");
    $stmt->execute([$ip, $phone, time() - RATE_LIMIT_WINDOW_SEC]);
    return $stmt->fetch()['c'] >= RATE_LIMIT_MAX;
}

function otp_rate_limited(string $phone): bool {
    $stmt = db()->prepare("SELECT COUNT(*) c FROM board WHERE type='log' AND phone = ? AND k2 = 'otp_requested' AND created_at > ?");
    $stmt->execute([$phone, time() - OTP_REQUEST_WINDOW_SEC]);
    return $stmt->fetch()['c'] >= OTP_REQUEST_MAX;
}

// ── Run sessions (type='run') — one-time tokens for a single game run ───
function issue_session(): array {
    $token = bin2hex(random_bytes(16));
    db()->prepare("INSERT INTO board (type, k2, t1, num, created_at) VALUES ('run', ?, ?, 0, ?)")
        ->execute([$token, time(), time()]);
    return ['ok' => true, 'token' => $token];
}

// Locks and consumes the token inside the caller's transaction. Returns the
// run's issued_at time, or null if the token is missing/used/expired.
function consume_session(PDO $pdo, string $token): ?int {
    $stmt = $pdo->prepare("SELECT id, t1, num FROM board WHERE type='run' AND k2 = ? FOR UPDATE");
    $stmt->execute([$token]);
    $row = $stmt->fetch();
    if (!$row || $row['num']) return null;
    if (time() - (int) $row['t1'] > SESSION_MAX_AGE) return null;
    $pdo->prepare('UPDATE board SET num = 1 WHERE id = ?')->execute([$row['id']]);
    return (int) $row['t1'];
}

// ── Player identity (type='otp', type='player', type='ident') ──────────

// Sends the OTP code. THIS IS A STUB — plug in your real SMS provider here.
// Until you do, codes are only written to the PHP error log (visible to
// you, the server admin) and never actually reach the player's phone. See
// README for a ready-to-use example (Kavenegar, a common Iranian gateway).
function send_otp_sms(string $phone, string $code): bool {
    /*
    $url = 'https://api.kavenegar.com/v1/YOUR_API_KEY/sms/send.json';
    $ch = curl_init($url);
    curl_setopt($ch, CURLOPT_POST, true);
    curl_setopt($ch, CURLOPT_POSTFIELDS, http_build_query([
        'receptor' => $phone,
        'message'  => "Your verification code: $code",
        'sender'   => 'YOUR_SENDER_NUMBER',
    ]));
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_TIMEOUT, 8);
    $ok = curl_exec($ch) !== false;
    curl_close($ch);
    return $ok;
    */
    error_log("[DEV MODE — no real SMS provider configured] OTP for $phone is $code");
    return true;
}

function request_otp(string $phone): array {
    if (otp_rate_limited($phone)) respond(['ok' => false, 'error' => 'too many codes requested, try later'], 429);

    $code = str_pad((string) random_int(0, 10 ** OTP_LENGTH - 1), OTP_LENGTH, '0', STR_PAD_LEFT);
    db()->prepare("
        INSERT INTO board (type, phone, k1, t1, num, created_at) VALUES ('otp', ?, ?, ?, 0, ?)
        ON DUPLICATE KEY UPDATE k1 = VALUES(k1), t1 = VALUES(t1), num = 0, created_at = VALUES(created_at)
    ")->execute([$phone, $code, time() + OTP_EXPIRE_SECONDS, time()]);

    log_attempt($phone, 0, client_ip(), true, 'otp_requested');
    send_otp_sms($phone, $code);
    return ['ok' => true];
}

// Picks two names + a random number, retrying on the astronomically rare
// chance of a collision. The database's UNIQUE constraint on type='player'
// rows is the real guarantee (see submit_score/verify_otp's catch block) —
// this loop just avoids unnecessary retries in the common case.
function random_username(PDO $pdo): string {
    $a = $b = NAME_POOL[0];
    for ($i = 0; $i < 20; $i++) {
        $a = NAME_POOL[array_rand(NAME_POOL)];
        $b = NAME_POOL[array_rand(NAME_POOL)];
        $name = $a . $b . random_int(10, 99);
        $stmt = $pdo->prepare("SELECT 1 FROM board WHERE type='player' AND k1 = ?");
        $stmt->execute([$name]);
        if (!$stmt->fetch()) return $name;
    }
    return $a . $b . bin2hex(random_bytes(3)); // practically unreachable fallback
}

function verify_otp(string $phone, string $code): array {
    $pdo = db();
    $pdo->beginTransaction();
    try {
        $stmt = $pdo->prepare("SELECT id, k1, t1, num FROM board WHERE type='otp' AND phone = ? FOR UPDATE");
        $stmt->execute([$phone]);
        $row = $stmt->fetch();

        if (!$row || time() > (int) $row['t1']) {
            $pdo->commit();
            respond(['ok' => false, 'error' => 'code expired'], 400);
        }
        if ((int) $row['num'] >= OTP_MAX_ATTEMPTS) {
            $pdo->commit();
            respond(['ok' => false, 'error' => 'too many attempts, request a new code'], 429);
        }
        if (!hash_equals($row['k1'], $code)) {
            $pdo->prepare('UPDATE board SET num = num + 1 WHERE id = ?')->execute([$row['id']]);
            $pdo->commit();
            respond(['ok' => false, 'error' => 'invalid code'], 400);
        }

        // Correct code — consume it so it can't be reused.
        $pdo->prepare('DELETE FROM board WHERE id = ?')->execute([$row['id']]);

        $stmt = $pdo->prepare("SELECT k1 FROM board WHERE type='player' AND phone = ?");
        $stmt->execute([$phone]);
        $existing = $stmt->fetch();

        if ($existing) {
            $username = $existing['k1'];
        } else {
            $username = random_username($pdo);
            $pdo->prepare("INSERT INTO board (type, phone, k1, score, t1, created_at) VALUES ('player', ?, ?, 0, ?, ?)")
                ->execute([$phone, $username, time(), time()]);
        }

        $identityToken = bin2hex(random_bytes(16));
        $pdo->prepare("INSERT INTO board (type, phone, k1, created_at) VALUES ('ident', ?, ?, ?)")
            ->execute([$phone, $identityToken, time()]);

        $pdo->commit();
        setcookie('player_id', $identityToken, time() + 60 * 60 * 24 * 365, '/', '', true, true);
        return ['ok' => true, 'identity_token' => $identityToken, 'username' => $username];
    } catch (Throwable $e) {
        $pdo->rollBack();
        // A UNIQUE constraint race on the player row (two people verifying
        // the exact same phone at the exact same instant) is the only
        // realistic cause here — extremely unlikely, but fail cleanly
        // rather than with a raw SQL error.
        respond(['ok' => false, 'error' => 'please try again'], 409);
    }
}

function identity_from_token(string $token): ?string {
    $stmt = db()->prepare("SELECT phone FROM board WHERE type='ident' AND k1 = ?");
    $stmt->execute([$token]);
    $row = $stmt->fetch();
    return $row ? $row['phone'] : null;
}

// ── Leaderboard reads ────────────────────────────────────────────────────
function get_top(int $limit): array {
    $stmt = db()->prepare("SELECT k1 AS username, score FROM board WHERE type='player' ORDER BY score DESC LIMIT ?");
    $stmt->bindValue(1, $limit, PDO::PARAM_INT);
    $stmt->execute();
    $top = [];
    foreach ($stmt->fetchAll() as $i => $row) {
        $top[] = ['rank' => $i + 1, 'username' => $row['username'], 'score' => (int) $row['score']];
    }
    return $top;
}

// ── Score submission ─────────────────────────────────────────────────────
function submit_score(string $identityToken, int $score, string $token, string $sig, string $ip): array {
    $phone = identity_from_token($identityToken);
    if ($phone === null) {
        log_attempt('unknown', $score, $ip, false, 'not_registered');
        respond(['ok' => false, 'error' => 'not_registered'], 400);
    }

    if (rate_limited($ip, $phone)) respond(['ok' => false, 'error' => 'too many submissions, slow down'], 429);

    $expected = hash_hmac('sha256', $token . ':' . $score, SIGN_KEY);
    if (!hash_equals($expected, $sig)) {
        log_attempt($phone, $score, $ip, false, 'bad signature');
        respond(['ok' => false, 'error' => 'invalid submission'], 400);
    }

    $pdo = db();
    $pdo->beginTransaction();
    try {
        $issuedAt = consume_session($pdo, $token);
        if ($issuedAt === null) {
            $pdo->commit();
            log_attempt($phone, $score, $ip, false, 'invalid or reused token');
            respond(['ok' => false, 'error' => 'invalid session'], 400);
        }

        $elapsed = max(1, time() - $issuedAt);
        if ($elapsed < MIN_RUN_SECONDS || $score > MAX_SCORE_PER_SEC * $elapsed + MAX_SCORE_BONUS) {
            $pdo->commit();
            log_attempt($phone, $score, $ip, false, 'implausible score');
            respond(['ok' => false, 'error' => 'score rejected'], 400);
        }

        $stmt = $pdo->prepare("SELECT id, score FROM board WHERE type='player' AND phone = ? FOR UPDATE");
        $stmt->execute([$phone]);
        $existing = $stmt->fetch();

        $updated = false;
        if (!$existing) {
            // Shouldn't normally happen (verify_otp always creates the row),
            // but handle it defensively rather than fatally erroring.
            $username = random_username($pdo);
            $pdo->prepare("INSERT INTO board (type, phone, k1, score, t1, created_at) VALUES ('player', ?, ?, ?, ?, ?)")
                ->execute([$phone, $username, $score, time(), time()]);
            $best = $score;
            $updated = true;
        } else {
            $best = (int) $existing['score'];
            if ($score > $best) {
                $pdo->prepare('UPDATE board SET score = ?, t1 = ? WHERE id = ?')->execute([$score, time(), $existing['id']]);
                $best = $score;
                $updated = true;
            }
        }

        $pdo->commit();
        log_attempt($phone, $score, $ip, true, $updated ? 'updated' : 'not a new best');

        $rankStmt = db()->prepare("SELECT COUNT(*) c FROM board WHERE type='player' AND score > ?");
        $rankStmt->execute([$best]);
        $rank = (int) $rankStmt->fetch()['c'] + 1;

        return ['ok' => true, 'updated' => $updated, 'rank' => $rank, 'best' => $best, 'top' => get_top(3)];
    } catch (Throwable $e) {
        $pdo->rollBack();
        throw $e;
    }
}

// ── Router ───────────────────────────────────────────────────────────────
cors();
header('Content-Type: application/json; charset=utf-8');

try {
    $method = $_SERVER['REQUEST_METHOD'] ?? 'GET';

    if ($method === 'GET' && ($_GET['action'] ?? 'top') === 'session') {
        respond(issue_session());

    } elseif ($method === 'GET') {
        $limit = max(1, min(TOP_LIMIT_MAX, (int) ($_GET['limit'] ?? 3)));
        respond(['ok' => true, 'top' => get_top($limit)]);

    } elseif ($method === 'POST') {
        $body = json_decode(file_get_contents('php://input'), true) ?: [];
        $action = $body['action'] ?? 'submit';

        if ($action === 'request_otp') {
            $phone = to_ascii_digits(trim((string) ($body['phone'] ?? '')));
            if (!valid_phone($phone)) respond(['ok' => false, 'error' => 'invalid phone'], 400);
            respond(request_otp($phone));

        } elseif ($action === 'verify_otp') {
            $phone = to_ascii_digits(trim((string) ($body['phone'] ?? '')));
            $code = to_ascii_digits(trim((string) ($body['code'] ?? '')));
            if (!valid_phone($phone)) respond(['ok' => false, 'error' => 'invalid phone'], 400);
            if (!preg_match('/^\d{' . OTP_LENGTH . '}$/', $code)) respond(['ok' => false, 'error' => 'invalid code'], 400);
            respond(verify_otp($phone, $code));

        } elseif ($action === 'submit') {
            $identityToken = (string) ($body['identity_token'] ?? '');
            $score = $body['score'] ?? null;
            $token = (string) ($body['token'] ?? '');
            $sig = (string) ($body['sig'] ?? '');

            if (strlen($identityToken) !== 32) respond(['ok' => false, 'error' => 'not_registered'], 400);
            if (!valid_score($score)) respond(['ok' => false, 'error' => 'invalid score'], 400);
            if (strlen($token) !== 32 || strlen($sig) !== 64) respond(['ok' => false, 'error' => 'invalid submission'], 400);

            respond(submit_score($identityToken, (int) round((float) $score), $token, $sig, client_ip()));

        } else {
            respond(['ok' => false, 'error' => 'unknown action'], 400);
        }

    } else {
        respond(['ok' => false, 'error' => 'method not allowed'], 405);
    }
} catch (Throwable $e) {
    respond(['ok' => false, 'error' => 'internal error'], 500);
}
