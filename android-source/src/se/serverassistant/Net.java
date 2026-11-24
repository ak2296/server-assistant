package se.serverassistant;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/** Plain-Java networking helpers (no Android classes, so they can be tested on a PC). */
final class Net {
    static final int PORT = 8765;

    private Net() { }

    static String normalize(String input) {
        String s = input == null ? "" : input.trim();
        if (s.isEmpty()) return null;
        if (!s.startsWith("http://") && !s.startsWith("https://")) s = "http://" + s;
        while (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        String afterScheme = s.substring(s.indexOf("://") + 3);
        if (!afterScheme.contains(":")) s = s + ":" + PORT;
        return s;
    }

    static boolean isServer(String base, int timeoutMs) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "/api/status").openConnection();
            c.setConnectTimeout(timeoutMs);
            c.setReadTimeout(timeoutMs);
            if (c.getResponseCode() != 200) return false;
            String body = readAll(c.getInputStream());
            return body.contains("release_check");
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** POST multipart/form-data to /api/downloads. Returns null on success, else an error message. */
    static String postDownload(String base, String link, String fileName, byte[] file, boolean music) throws Exception {
        String boundary = "----sa" + System.currentTimeMillis();
        HttpURLConnection c = (HttpURLConnection) new URL(base + "/api/downloads").openConnection();
        c.setConnectTimeout(8000);
        c.setReadTimeout(20000);
        c.setDoOutput(true);
        c.setRequestMethod("POST");
        c.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
        DataOutputStream out = new DataOutputStream(c.getOutputStream());
        field(out, boundary, "links", link == null ? "" : link);
        field(out, boundary, "music", music ? "true" : "false");
        if (file != null) {
            out.writeBytes("--" + boundary + "\r\n");
            out.write(("Content-Disposition: form-data; name=\"files\"; filename=\""
                    + fileName.replace("\"", "") + "\"\r\n").getBytes("UTF-8"));
            out.writeBytes("Content-Type: application/x-bittorrent\r\n\r\n");
            out.write(file);
            out.writeBytes("\r\n");
        }
        out.writeBytes("--" + boundary + "--\r\n");
        out.flush();
        out.close();
        int code = c.getResponseCode();
        String body = readAll(code < 400 ? c.getInputStream() : c.getErrorStream());
        c.disconnect();
        if (code < 300) return null;
        int i = body.indexOf("\"detail\":\"");
        if (i >= 0) {
            String msg = body.substring(i + 10);
            return msg.substring(0, Math.max(0, msg.indexOf('"')));
        }
        return "Server said " + code;
    }

    private static void field(DataOutputStream out, String boundary, String name, String value) throws Exception {
        out.writeBytes("--" + boundary + "\r\n");
        out.writeBytes("Content-Disposition: form-data; name=\"" + name + "\"\r\n\r\n");
        out.write(value.getBytes("UTF-8"));
        out.writeBytes("\r\n");
    }

    static byte[] readBytes(InputStream in) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[16384];
        int n;
        while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
        in.close();
        return bos.toByteArray();
    }

    static String readAll(InputStream in) throws Exception {
        if (in == null) return "";
        return new String(readBytes(in), "UTF-8");
    }

}
