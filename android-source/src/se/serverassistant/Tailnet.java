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

    /** Address the helper listens on. The port is picked fresh each time (a leftover helper can never block it). */
    static volatile String LOCAL = "http://127.0.0.1:8766";
    private static Process PROC;   // the one helper of this app process

    private final Context ctx;
    private final Listener listener;

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
        Process p = PROC;
        PROC = null;
        if (p != null) p.destroy();
    }

    private static int freePort() {
        try {
            java.net.ServerSocket ss = new java.net.ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"));
            int port = ss.getLocalPort();
            ss.close();
            return port;
        } catch (Exception e) {
            return 18766;
        }
    }

    private void run2() {
        // already running from earlier (the helper can outlive the screen)
        if (PROC != null && Net.isServer(LOCAL, 1500)) { listener.onUp(); return; }
        stop();   // a helper that is still waiting for sign-in: start over cleanly instead of fighting for the port
        final String listen = "127.0.0.1:" + freePort();
        LOCAL = "http://" + listen;
        StringBuilder tail = new StringBuilder();
        try {
            File dir = new File(ctx.getFilesDir(), "tailnet");
            dir.mkdirs();
            String name = "phone-" + Build.MODEL.toLowerCase().replaceAll("[^a-z0-9]+", "-");
            ProcessBuilder pb = new ProcessBuilder(helper(ctx).getPath(), "-mode", "connect", "-dir", dir.getPath(),
                    "-hostname", name, "-remote", ctx.getSharedPreferences("settings", Context.MODE_PRIVATE).getString("tsname", "serverassistant"), "-listen", listen, "-exit-with-parent");
            pb.redirectErrorStream(true);
            pb.environment().put("HOME", dir.getPath());
            pb.environment().put("TMPDIR", ctx.getCacheDir().getPath());
            pb.environment().put("SSL_CERT_DIR", "/system/etc/security/cacerts");
            final Process mine = pb.start();
            PROC = mine;
            BufferedReader r = new BufferedReader(new InputStreamReader(mine.getInputStream()));
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
