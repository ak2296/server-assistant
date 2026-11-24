package se.serverassistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.PendingIntent;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.os.Build;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Keeps the app up to date from the project's GitHub release (the same place the server updates from).
 * Android never lets an app replace itself without asking once, so the person taps "Update" and then "Install".
 */
final class Updater {
    private static final String REPO = "ak2296/server-assistant";
    private static final String TAG = "app-latest";
    private static final long EVERY_MS = 6L * 3600 * 1000;

    private Updater() { }

    static String current(Context c) {
        try { return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).versionName; } catch (Exception e) { return "0"; }
    }

    static boolean newer(String latest, String have) {
        String[] a = latest.replaceAll("[^0-9.]", "").split("\\."), b = have.replaceAll("[^0-9.]", "").split("\\.");
        for (int i = 0; i < Math.max(a.length, b.length); i++) {
            int x = i < a.length && !a[i].isEmpty() ? Integer.parseInt(a[i]) : 0;
            int y = i < b.length && !b[i].isEmpty() ? Integer.parseInt(b[i]) : 0;
            if (x != y) return x > y;
        }
        return false;
    }

    /** Looks at GitHub (at most every 6 hours unless forced) and asks the person if a newer app exists. */
    static void check(final Activity act, final SharedPreferences prefs, final boolean force) {
        long last = prefs.getLong("update_checked", 0);
        if (!force && System.currentTimeMillis() - last < EVERY_MS) return;
        new Thread(new Runnable() {
            public void run() {
                try {
                    HttpURLConnection c = (HttpURLConnection) new URL("https://api.github.com/repos/" + REPO + "/releases/tags/" + TAG).openConnection();
                    c.setConnectTimeout(8000);
                    c.setReadTimeout(8000);
                    c.setRequestProperty("User-Agent", "ServerAssistantApp");
                    if (c.getResponseCode() != 200) return;
                    final String latest = new JSONObject(Net.readAll(c.getInputStream())).optString("name", "");
                    prefs.edit().putLong("update_checked", System.currentTimeMillis()).apply();
                    final String have = current(act);
                    if (latest.isEmpty() || !newer(latest, have)) {
                        if (force) toast(act, "The app is up to date (" + have + ").");
                        return;
                    }
                    act.runOnUiThread(new Runnable() {
                        public void run() { ask(act, latest.replace("v", ""), have); }
                    });
                } catch (Exception e) {
                    if (force) toast(act, "Couldn't check for updates: " + e.getMessage());
                }
            }
        }).start();
    }

    private static void ask(final Activity act, String latest, String have) {
        if (act.isFinishing()) return;
        new AlertDialog.Builder(act, android.R.style.Theme_Material_Dialog_Alert)
                .setTitle("Update available")
                .setMessage("Server Assistant " + latest + " is ready (you have " + have + ").")
                .setPositiveButton("Update", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int w) { start(act); }
                })
                .setNegativeButton("Later", null)
                .show();
    }

    private static void start(final Activity act) {
        if (Build.VERSION.SDK_INT >= 26 && !canInstall(act)) {
            toast(act, "Allow Server Assistant to install updates, then press Update again.");
            try {
                act.startActivity(new Intent("android.settings.MANAGE_UNKNOWN_APP_SOURCES", Uri.parse("package:" + act.getPackageName())));
            } catch (Exception ignored) { }
            return;
        }
        toast(act, "Downloading the update…");
        new Thread(new Runnable() {
            public void run() {
                try {
                    File f = new File(act.getCacheDir(), "update.apk");
                    HttpURLConnection c = (HttpURLConnection) new URL("https://github.com/" + REPO + "/releases/download/" + TAG + "/ServerAssistant.apk").openConnection();
                    c.setConnectTimeout(10000);
                    c.setReadTimeout(30000);
                    InputStream in = c.getInputStream();
                    OutputStream out = new FileOutputStream(f);
                    byte[] buf = new byte[65536];
                    int n;
                    while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                    out.close();
                    in.close();
                    install(act, f);
                } catch (Exception e) {
                    toast(act, "Couldn't download the update: " + e.getMessage());
                }
            }
        }).start();
    }

    private static boolean canInstall(Context c) {  // API 26+: may this app install packages? (looked up at run time so older phones still work)
        try { return (Boolean) c.getPackageManager().getClass().getMethod("canRequestPackageInstalls").invoke(c.getPackageManager()); }
        catch (Throwable t) { return true; }
    }

    private static void install(Context ctx, File apk) throws Exception {
        PackageInstaller pi = ctx.getPackageManager().getPackageInstaller();
        PackageInstaller.SessionParams p = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        p.setAppPackageName(ctx.getPackageName());
        try {  // Android 12+: an app that installed itself before can update without another prompt
            PackageInstaller.SessionParams.class.getMethod("setRequireUserAction", int.class).invoke(p, 2);
        } catch (Throwable ignored) { }
        int id = pi.createSession(p);
        PackageInstaller.Session s = pi.openSession(id);
        OutputStream out = s.openWrite("app", 0, apk.length());
        InputStream in = new java.io.FileInputStream(apk);
        byte[] buf = new byte[65536];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        in.close();
        s.fsync(out);
        out.close();
        Intent i = new Intent(ctx, UpdateReceiver.class).setAction("se.serverassistant.INSTALL");
        PendingIntent pend = PendingIntent.getBroadcast(ctx, id, i, 33554432 | 134217728);  // FLAG_MUTABLE | FLAG_UPDATE_CURRENT
        s.commit(pend.getIntentSender());
    }

    static void toast(final Context c, final String m) {
        if (c instanceof Activity) ((Activity) c).runOnUiThread(new Runnable() {
            public void run() { Toast.makeText(c, m, Toast.LENGTH_LONG).show(); }
        });
    }
}
