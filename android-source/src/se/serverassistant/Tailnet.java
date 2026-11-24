package se.serverassistant;

import android.content.Context;
import android.os.Build;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;

/**
 * Away from home: runs the small Tailscale helper that ships inside the app (the same program the PC uses)
 * in "connect" mode. It opens 127.0.0.1:8766 and passes connections through your private network to the server.
 * It never changes the phone's own network or VPN settings.
 */
final class Tailnet {
    interface Listener {
        void onLogin(String url);
        void onUp();
        void onError(String message);
    }

    static final String LOCAL = "http://127.0.0.1:8766";

    private final Context ctx;
    private final Listener listener;
    private Process proc;

    Tailnet(Context ctx, Listener l) {
        this.ctx = ctx.getApplicationContext();
        this.listener = l;
    }

    static File helper(Context c) {
        return new File(c.getApplicationInfo().nativeLibraryDir, "libsatailnet.so");
    }

    static boolean available(Context c) {
        return helper(c).exists();
    }

    void start() {
        new Thread(new Runnable() {
            public void run() { run2(); }
        }).start();
    }

    void stop() {
        if (proc != null) proc.destroy();
        proc = null;
    }

    private void run2() {
        // already running from earlier (the helper can outlive the screen)
        if (Net.isServer(LOCAL, 1500)) { listener.onUp(); return; }
        StringBuilder tail = new StringBuilder();
        try {
            File dir = new File(ctx.getFilesDir(), "tailnet");
            dir.mkdirs();
            String name = "phone-" + Build.MODEL.toLowerCase().replaceAll("[^a-z0-9]+", "-");
            ProcessBuilder pb = new ProcessBuilder(helper(ctx).getPath(), "-mode", "connect", "-dir", dir.getPath(),
                    "-hostname", name, "-remote", "serverassistant", "-listen", "127.0.0.1:8766");
            pb.redirectErrorStream(true);
            pb.environment().put("HOME", dir.getPath());
            pb.environment().put("TMPDIR", ctx.getCacheDir().getPath());
            pb.environment().put("SSL_CERT_DIR", "/system/etc/security/cacerts");
            proc = pb.start();
            BufferedReader r = new BufferedReader(new InputStreamReader(proc.getInputStream()));
            String line;
            while ((line = r.readLine()) != null) {
                JSONObject j;
                try { j = new JSONObject(line); } catch (Exception e) {
                    tail.append(line).append('\n');
                    if (tail.length() > 600) tail.delete(0, tail.length() - 600);
                    continue;
                }
                String state = j.optString("state");
                if ("login".equals(state)) listener.onLogin(j.optString("url"));
                else if ("up".equals(state)) listener.onUp();
                else if ("error".equals(state)) { listener.onError(j.optString("error")); return; }
            }
            listener.onError("The connection helper stopped." + (tail.length() > 0 ? "\n" + tail : ""));
        } catch (Exception e) {
            listener.onError("Could not start the connection helper: " + e.getMessage());
        }
    }
}
