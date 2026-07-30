<?php
/**
 * Report a partner's feed total to the ops dashboard.
 *
 * Copy this file to the partner servers (e.g. /home/fcampbell/report_to_dashboard.php)
 * and add TWO lines to the end of any insertEvent*.php:
 *
 *     require_once '/home/fcampbell/report_to_dashboard.php';
 *     ops_report('bokun', $total_in_feed, $inserted_count);
 *
 * $total_in_feed is however many records the feed/API actually had - the script
 * already knows this, it is the size of the array or row count it looped over.
 * That number exists nowhere in MySQL, which is why the dashboard needs it sent.
 *
 * Safe to call unconditionally: it never throws, never blocks for more than a
 * couple of seconds, and a dashboard that is down cannot affect the ingest run.
 */

function ops_report($partner, $feed_count, $inserted = null, $note = null) {
    // The dashboard is reached through the SSH reverse tunnel opened by
    // run-with-tunnel.sh. Override with OPS_DASHBOARD_URL if it moves.
    $url = getenv('OPS_DASHBOARD_URL')
         ?: 'http://127.0.0.1:8777/api/partners/feed-count';
    $secret = getenv('OPS_AGENT_SECRET') ?: 'change-this-to-a-secure-token';

    $payload = json_encode([
        'partner'    => $partner,
        'feed_count' => $feed_count === null ? null : (int) $feed_count,
        'inserted'   => $inserted === null ? null : (int) $inserted,
        'source'     => 'script',
        'note'       => $note,
    ]);

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => $payload,
        CURLOPT_RETURNTRANSFER => true,
        // Short timeouts on purpose: reporting is best-effort and must never
        // hold up or fail the actual ingest job.
        CURLOPT_CONNECTTIMEOUT => 3,
        CURLOPT_TIMEOUT        => 5,
        CURLOPT_HTTPHEADER     => [
            'Content-Type: application/json',
            'x-agent-secret: ' . $secret,
        ],
    ]);
    $response = curl_exec($ch);
    $status   = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err      = curl_error($ch);
    curl_close($ch);

    if ($status === 200) {
        echo "[ops-dashboard] reported {$partner}: feed={$feed_count} inserted={$inserted}\n";
        return true;
    }
    // A 401 means OPS_AGENT_SECRET does not match the dashboard's.
    echo "[ops-dashboard] report failed for {$partner}"
       . ($status ? " (HTTP {$status})" : " ({$err})") . "\n";
    return false;
}
