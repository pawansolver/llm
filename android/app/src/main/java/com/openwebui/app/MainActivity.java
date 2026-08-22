package com.openwebui.app;

import android.Manifest;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;

import androidx.annotation.NonNull;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    private static final int PERMISSION_REQUEST_CODE = 100;

    // Holds the pending WebView permission request while we ask Android for
    // the corresponding runtime permissions.
    private PermissionRequest pendingWebPermissionRequest;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Allow audio/video playback without requiring an explicit user tap.
        WebView webView = getBridge().getWebView();
        WebSettings settings = webView.getSettings();
        settings.setMediaPlaybackRequiresUserGesture(false);

        // Install our media-permission WebChromeClient immediately in onCreate()
        // so that it is in place before any JavaScript executes.  If we wait
        // until onStart() there is a race: the page can finish loading and call
        // getUserMedia() before our client is installed, causing the permission
        // request to be silently ignored by Capacitor's default client.
        installMediaPermissionClient();
    }

    /**
     * Install our custom WebChromeClient in onStart(), AFTER Capacitor's
     * bridge has fully initialised and set its own client.  If we do this in
     * onCreate() the bridge will overwrite us.  We wrap Capacitor's client so
     * that every callback we don't explicitly handle is delegated to it,
     * ensuring file-chooser and other WebView features keep working.
     */
    @Override
    public void onStart() {
        super.onStart();
        installMediaPermissionClient();
    }

    @Override
    public void onResume() {
        super.onResume();
        installMediaPermissionClient();
    }

    private void installMediaPermissionClient() {
        WebView webView = getBridge().getWebView();

        // Grab whatever client Capacitor installed so we can delegate to it.
        final WebChromeClient capacitorClient = webView.getWebChromeClient();

        webView.setWebChromeClient(new WebChromeClient() {

            // ------------------------------------------------------------------
            // Mic / Camera permission requests from JavaScript (getUserMedia)
            // ------------------------------------------------------------------
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                boolean needsMic    = false;
                boolean needsCamera = false;

                for (String resource : request.getResources()) {
                    if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) needsMic    = true;
                    if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(resource)) needsCamera = true;
                }

                boolean micGranted = ContextCompat.checkSelfPermission(
                        MainActivity.this, Manifest.permission.RECORD_AUDIO)
                        == PackageManager.PERMISSION_GRANTED;
                boolean camGranted = ContextCompat.checkSelfPermission(
                        MainActivity.this, Manifest.permission.CAMERA)
                        == PackageManager.PERMISSION_GRANTED;

                boolean micOk = !needsMic || micGranted;
                boolean camOk = !needsCamera || camGranted;

                if (micOk && camOk) {
                    // All required Android runtime permissions are already granted
                    // — hand the resource directly back to the WebView.
                    request.grant(request.getResources());
                } else {
                    // Need to ask the user for one or more runtime permissions.
                    pendingWebPermissionRequest = request;

                    String[] perms;
                    if (!micGranted && !camGranted) {
                        perms = new String[]{
                                Manifest.permission.RECORD_AUDIO,
                                Manifest.permission.CAMERA};
                    } else if (!micGranted) {
                        perms = new String[]{Manifest.permission.RECORD_AUDIO};
                    } else {
                        perms = new String[]{Manifest.permission.CAMERA};
                    }

                    ActivityCompat.requestPermissions(
                            MainActivity.this, perms, PERMISSION_REQUEST_CODE);
                }
            }

            // ------------------------------------------------------------------
            // File chooser — delegate to Capacitor so file uploads still work
            // ------------------------------------------------------------------
            @Override
            public boolean onShowFileChooser(WebView wv,
                                             ValueCallback<Uri[]> filePathCallback,
                                             FileChooserParams fileChooserParams) {
                if (capacitorClient != null) {
                    return capacitorClient.onShowFileChooser(
                            wv, filePathCallback, fileChooserParams);
                }
                return super.onShowFileChooser(wv, filePathCallback, fileChooserParams);
            }

            // ------------------------------------------------------------------
            // Progress / Title — delegate so Capacitor internals still work
            // ------------------------------------------------------------------
            @Override
            public void onProgressChanged(WebView wv, int newProgress) {
                if (capacitorClient != null) {
                    capacitorClient.onProgressChanged(wv, newProgress);
                } else {
                    super.onProgressChanged(wv, newProgress);
                }
            }

            @Override
            public void onReceivedTitle(WebView wv, String title) {
                if (capacitorClient != null) {
                    capacitorClient.onReceivedTitle(wv, title);
                } else {
                    super.onReceivedTitle(wv, title);
                }
            }
        });
    }

    // --------------------------------------------------------------------------
    // Called by Android after the user responds to the runtime permission dialog
    // --------------------------------------------------------------------------
    @Override
    public void onRequestPermissionsResult(int requestCode,
                                           @NonNull String[] permissions,
                                           @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);

        if (requestCode == PERMISSION_REQUEST_CODE && pendingWebPermissionRequest != null) {
            boolean allGranted = true;
            for (int result : grantResults) {
                if (result != PackageManager.PERMISSION_GRANTED) {
                    allGranted = false;
                    break;
                }
            }

            if (allGranted) {
                // User granted the runtime permission — now grant the WebView resource.
                pendingWebPermissionRequest.grant(pendingWebPermissionRequest.getResources());
            } else {
                // User denied — deny the WebView request so JS gets a clean error.
                pendingWebPermissionRequest.deny();
            }
            pendingWebPermissionRequest = null;
        }
    }
}
