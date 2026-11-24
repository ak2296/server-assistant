package se.serverassistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.OpenableColumns;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.InputStream;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * Server Assistant for Android.
 * - First run: enter the server address or let the app find it on the Wi-Fi.
 * - Shows the dashboard full screen.
 * - Tapping a magnet link / opening a .torrent file / sharing one to the app
 *   sends it straight to the server's download queue.
 */
public class MainActivity extends Activity {
    private static final int PORT = 8765;
    private static final int FILE_CHOOSER = 1;
    private static final int BG = Color.parseColor("#111317");
    private static final int CARD = Color.parseColor("#1B1E24");
    private static final int TEXT = Color.parseColor("#E8E9EC");
    private static final int MUTED = Color.parseColor("#8B909A");
    private static final int ACCENT = Color.parseColor("#E5A00D");

    private SharedPreferences prefs;
    private WebView web;
    private ValueCallback<Uri[]> fileCallback;
    private Intent pendingIntent;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("settings", MODE_PRIVATE);
        pendingIntent = getIntent();
        if (server() == null) showSetup(null);
        else showDashboard("");
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        pendingIntent = intent;
        if (server() != null) handleIncoming();
    }

    private String server() {
        return prefs.getString("server", null);
    }

    private int dp(float v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    // ------------------------------------------------------------------ setup screen

    private void showSetup(String message) {
        if (web != null) { web.destroy(); web = null; }
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(BG);
        scroll.setFillViewport(true);
        final LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(24), dp(48), dp(24), dp(24));
        scroll.addView(box);

        TextView title = text("Server Assistant", 26, TEXT);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        box.addView(title);
        TextView intro = text("Connect to the Server Assistant running on your media PC. "
                + "Your phone has to be on the same Wi-Fi.", 15, MUTED);
        intro.setPadding(0, dp(8), 0, dp(24));
        box.addView(intro);

        final EditText address = new EditText(this);
        address.setHint("e.g. 192.168.1.20:8765");
        address.setHintTextColor(MUTED);
        address.setTextColor(TEXT);
        address.setSingleLine(true);
        address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        address.setImeOptions(EditorInfo.IME_ACTION_GO);
        address.setBackground(rounded(CARD, 12));
        address.setPadding(dp(14), dp(12), dp(14), dp(12));
        String saved = server();
        if (saved != null) address.setText(saved.replace("http://", ""));
        box.addView(address, matchWrap());

        final TextView status = text(message == null ? "" : message, 14, message == null ? MUTED : Color.parseColor("#FF9B97"));
        status.setPadding(0, dp(10), 0, dp(10));

        Button connect = button("Connect", true);
        connect.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { tryConnect(address.getText().toString(), status); }
        });
        address.setOnEditorActionListener(new TextView.OnEditorActionListener() {
            public boolean onEditorAction(TextView v, int id, KeyEvent e) {
                tryConnect(address.getText().toString(), status);
                return true;
            }
        });
        LinearLayout.LayoutParams lp = matchWrap();
        lp.topMargin = dp(12);
        box.addView(connect, lp);
        box.addView(status);

        final LinearLayout found = new LinearLayout(this);
        found.setOrientation(LinearLayout.VERTICAL);
        final ProgressBar spin = new ProgressBar(this);
        spin.setVisibility(View.GONE);

        Button find = button("Find my server automatically", false);
        find.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { discover(status, found, spin); }
        });
        box.addView(find, matchWrap());
        box.addView(spin);
        box.addView(found);

        setContentView(scroll);
        if (saved == null) discover(status, found, spin);
    }

    private void tryConnect(String input, final TextView status) {
        final String url = Net.normalize(input);
        if (url == null) { status.setText("Type the address shown in the server window."); return; }
        status.setTextColor(MUTED);
        status.setText("Connecting to " + url + " …");
        new Thread(new Runnable() {
            public void run() {
                final boolean ok = Net.isServer(url, 4000);
                ui.post(new Runnable() {
                    public void run() {
                        if (ok) useServer(url);
                        else {
                            status.setTextColor(Color.parseColor("#FF9B97"));
                            status.setText("Couldn't reach Server Assistant at " + url
                                    + ".\nIs start.bat running on the server, and is the phone on the same Wi-Fi?");
                        }
                    }
                });
            }
        }).start();
    }

    private void useServer(String url) {
        prefs.edit().putString("server", url).apply();
        showDashboard("");
    }

    /** Try every address on the phone's Wi-Fi subnet on port 8765. */
    private void discover(final TextView status, final LinearLayout found, final ProgressBar spin) {
        final List<String> prefixes = localPrefixes();
        found.removeAllViews();
        if (prefixes.isEmpty()) {
            status.setText("Not connected to Wi-Fi. Connect to your home network and try again.");
            return;
        }
        status.setTextColor(MUTED);
        status.setText("Searching your network…");
        spin.setVisibility(View.VISIBLE);
        new Thread(new Runnable() {
            public void run() {
                final List<String> hits = Collections.synchronizedList(new ArrayList<String>());
                ExecutorService pool = Executors.newFixedThreadPool(48);
                for (String prefix : prefixes) {
                    for (int i = 1; i < 255; i++) {
                        final String url = "http://" + prefix + i + ":" + PORT;
                        pool.execute(new Runnable() {
                            public void run() { if (Net.isServer(url, 700)) hits.add(url); }
                        });
                    }
                }
                pool.shutdown();
                try { pool.awaitTermination(40, TimeUnit.SECONDS); } catch (InterruptedException ignored) { }
                ui.post(new Runnable() {
                    public void run() {
                        spin.setVisibility(View.GONE);
                        if (hits.isEmpty()) {
                            status.setText("No server found. Check that start.bat is running on the server and that Windows Firewall allows it, or type the address above.");
                        } else if (hits.size() == 1 && server() == null) {
                            useServer(hits.get(0));
                        } else {
                            status.setText("Found:");
                            for (final String h : hits) {
                                Button b = button(h.replace("http://", ""), false);
                                b.setOnClickListener(new View.OnClickListener() {
                                    public void onClick(View v) { useServer(h); }
                                });
                                found.addView(b, matchWrap());
                            }
                        }
                    }
                });
            }
        }).start();
    }

    private static List<String> localPrefixes() {
        List<String> out = new ArrayList<String>();
        try {
            Enumeration<NetworkInterface> ifs = NetworkInterface.getNetworkInterfaces();
            while (ifs != null && ifs.hasMoreElements()) {
                NetworkInterface ni = ifs.nextElement();
                if (!ni.isUp() || ni.isLoopback()) continue;
                String name = ni.getName().toLowerCase();
                if (name.startsWith("rmnet") || name.startsWith("ccmni") || name.startsWith("tun")) continue; // mobile data / VPN
                Enumeration<InetAddress> addrs = ni.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    InetAddress a = addrs.nextElement();
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) {
                        String ip = a.getHostAddress();
                        String prefix = ip.substring(0, ip.lastIndexOf('.') + 1);
                        if (!out.contains(prefix)) out.add(prefix);
                    }
                }
            }
        } catch (Exception ignored) { }
        return out;
    }

    // ------------------------------------------------------------------ dashboard

    private void showDashboard(String hash) {
        web = new WebView(this);
        web.setBackgroundColor(BG);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(true);
        s.setUserAgentString(s.getUserAgentString() + " ServerAssistantApp/1.0");
        web.addJavascriptInterface(new Bridge(), "Android");

        web.setWebViewClient(new WebViewClient() {
            @Override
            @SuppressWarnings("deprecation")
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return openOutside(Uri.parse(url));
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) showOffline();
            }

            @Override
            @SuppressWarnings("deprecation")
            public void onReceivedError(WebView view, int code, String desc, String url) {
                if (url != null && url.startsWith(server())) showOffline();
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> cb, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = cb;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("*/*");  // many file managers don't label .torrent files correctly
                i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE);
                try {
                    startActivityForResult(Intent.createChooser(i, "Choose .torrent file"), FILE_CHOOSER);
                } catch (Exception e) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }
        });

        setContentView(web);
        web.loadUrl(server() + "/" + (hash.isEmpty() ? "" : "#" + hash));
        handleIncoming();
    }

    private boolean openOutside(Uri uri) {
        String base = server();
        if (base != null && uri.toString().startsWith(base)) return false;  // stay inside the app
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (Exception ignored) { }
        return true;
    }

    private void showOffline() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setBackgroundColor(BG);
        box.setPadding(dp(32), dp(32), dp(32), dp(32));
        TextView t = text("Can't reach your server", 20, TEXT);
        t.setTypeface(Typeface.DEFAULT_BOLD);
        t.setGravity(Gravity.CENTER);
        box.addView(t);
        TextView d = text("Make sure the server PC is on, start.bat is running, and your phone is on the home Wi-Fi.\n\n" + server(), 14, MUTED);
        d.setGravity(Gravity.CENTER);
        d.setPadding(0, dp(10), 0, dp(24));
        box.addView(d);
        Button retry = button("Try again", true);
        retry.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { showDashboard(""); }
        });
        box.addView(retry, matchWrap());
        Button change = button("Change server", false);
        change.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { showSetup(null); }
        });
        LinearLayout.LayoutParams lp = matchWrap();
        lp.topMargin = dp(10);
        box.addView(change, lp);
        if (web != null) { web.destroy(); web = null; }
        setContentView(box);
    }

    /** Called from the dashboard page (window.Android). */
    private class Bridge {
        @JavascriptInterface
        public void changeServer() {
            ui.post(new Runnable() { public void run() { showSetup(null); } });
        }

        @JavascriptInterface
        public String version() { return "1.0"; }
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        if (request == FILE_CHOOSER && fileCallback != null) {
            Uri[] uris = null;
            if (result == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    uris = new Uri[data.getClipData().getItemCount()];
                    for (int i = 0; i < uris.length; i++) uris[i] = data.getClipData().getItemAt(i).getUri();
                } else if (data.getData() != null) {
                    uris = new Uri[]{data.getData()};
                }
            }
            fileCallback.onReceiveValue(uris);
            fileCallback = null;
            return;
        }
        super.onActivityResult(request, result, data);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    // ------------------------------------------------------------------ magnet / .torrent from other apps

    private void handleIncoming() {
        Intent intent = pendingIntent;
        pendingIntent = null;
        if (intent == null || server() == null) return;
        String action = intent.getAction();
        String magnet = null;
        Uri file = null;

        if (Intent.ACTION_VIEW.equals(action) && intent.getData() != null) {
            Uri u = intent.getData();
            if ("magnet".equals(u.getScheme())) magnet = u.toString();
            else file = u;
        } else if (Intent.ACTION_SEND.equals(action)) {
            String txt = intent.getStringExtra(Intent.EXTRA_TEXT);
            if (txt != null) {
                int at = txt.indexOf("magnet:?");
                if (at >= 0) {
                    String rest = txt.substring(at);
                    int end = 0;
                    while (end < rest.length() && !Character.isWhitespace(rest.charAt(end))) end++;
                    magnet = rest.substring(0, end);
                } else if (txt.trim().startsWith("http") && txt.trim().contains(".torrent")) {
                    magnet = txt.trim();  // a link to a .torrent file
                }
            }
            Object stream = intent.getParcelableExtra(Intent.EXTRA_STREAM);
            if (magnet == null && stream instanceof Uri) file = (Uri) stream;
        }
        if (magnet == null && file == null) {
            if (Intent.ACTION_SEND.equals(action)) toast("Nothing to download in what was shared.");
            return;
        }
        askAndSend(magnet, file);
    }

    private void askAndSend(final String link, final Uri file) {
        String what = link != null ? shortName(link) : fileName(file);
        new AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
                .setTitle("Download on server?")
                .setMessage(what)
                .setPositiveButton("Music (sort into library)", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int w) { send(link, file, true); }
                })
                .setNeutralButton("Other", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int w) { send(link, file, false); }
                })
                .setNegativeButton("Cancel", null)
                .show();
    }

    private static String shortName(String magnet) {
        int i = magnet.indexOf("dn=");
        if (i < 0) return "Magnet link";
        String name = magnet.substring(i + 3);
        int amp = name.indexOf('&');
        if (amp >= 0) name = name.substring(0, amp);
        try { return java.net.URLDecoder.decode(name, "UTF-8"); } catch (Exception e) { return name; }
    }

    private String fileName(Uri uri) {
        String name = null;
        try {
            Cursor c = getContentResolver().query(uri, null, null, null, null);
            if (c != null) {
                int idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (idx >= 0 && c.moveToFirst()) name = c.getString(idx);
                c.close();
            }
        } catch (Exception ignored) { }
        if (name == null) name = uri.getLastPathSegment();
        return name == null ? "file.torrent" : name;
    }

    private void send(final String link, final Uri file, final boolean music) {
        toast("Sending to server…");
        new Thread(new Runnable() {
            public void run() {
                String error = null;
                try {
                    byte[] content = null;
                    String name = null;
                    if (file != null) {
                        InputStream in = getContentResolver().openInputStream(file);
                        content = Net.readBytes(in);
                        name = fileName(file);
                        if (!name.toLowerCase().endsWith(".torrent")) name = name + ".torrent";
                    }
                    error = Net.postDownload(server(), link, name, content, music);
                } catch (Exception e) {
                    error = "Couldn't send: " + e.getMessage();
                }
                final String err = error;
                ui.post(new Runnable() {
                    public void run() {
                        if (err == null) {
                            toast("Download started");
                            if (web != null) web.loadUrl(server() + "/#downloads");
                            else showDashboard("downloads");
                        } else {
                            toast(err);
                        }
                    }
                });
            }
        }).start();
    }

    // ------------------------------------------------------------------ helpers

    private void toast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show();
    }

    private TextView text(String s, int sp, int color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(color);
        return t;
    }

    private Button button(String label, boolean primary) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        b.setTextColor(primary ? Color.parseColor("#1A1300") : TEXT);
        b.setBackground(rounded(primary ? ACCENT : CARD, 12));
        b.setPadding(dp(16), dp(12), dp(16), dp(12));
        b.setStateListAnimator(null);
        return b;
    }

    private GradientDrawable rounded(int color, int radiusDp) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(color);
        g.setCornerRadius(dp(radiusDp));
        return g;
    }

    private LinearLayout.LayoutParams matchWrap() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = dp(6);
        return lp;
    }
}
