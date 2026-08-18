package com.openwebui.app;

import android.Manifest;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebView;

import androidx.annotation.NonNull;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    private static final int PERMISSION_REQUEST_CODE = 100;
    private PermissionRequest pendingWebPermissionRequest;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Set a custom WebChromeClient to handle microphone/camera permission requests
        // from the WebView (required for navigator.mediaDevices.getUserMedia to work)
        WebView webView = getBridge().getWebView();
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                // Check which permissions the web page is requesting
                boolean needsMic = false;
                boolean needsCamera = false;

                for (String resource : request.getResources()) {
                    if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) needsMic = true;
                    if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(resource)) needsCamera = true;
                }

                // Check if Android-level permissions are already granted
                boolean micGranted = ContextCompat.checkSelfPermission(
                        MainActivity.this, Manifest.permission.RECORD_AUDIO)
                        == PackageManager.PERMISSION_GRANTED;
                boolean camGranted = ContextCompat.checkSelfPermission(
                        MainActivity.this, Manifest.permission.CAMERA)
                        == PackageManager.PERMISSION_GRANTED;

                if ((needsMic && !micGranted) || (needsCamera && !camGranted)) {
                    // Request Android-level permissions first, then grant to WebView
                    pendingWebPermissionRequest = request;
                    String[] perms = needsMic && needsCamera
                            ? new String[]{Manifest.permission.RECORD_AUDIO, Manifest.permission.CAMERA}
                            : needsMic
                                ? new String[]{Manifest.permission.RECORD_AUDIO}
                                : new String[]{Manifest.permission.CAMERA};
                    ActivityCompat.requestPermissions(MainActivity.this, perms, PERMISSION_REQUEST_CODE);
                } else {
                    // Permissions already granted — allow the WebView request directly
                    request.grant(request.getResources());
                }
            }
        });
    }

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
                // Grant all resources to WebView after user allowed
                pendingWebPermissionRequest.grant(pendingWebPermissionRequest.getResources());
            } else {
                // User denied — deny the WebView request too
                pendingWebPermissionRequest.deny();
            }
            pendingWebPermissionRequest = null;
        }
    }
}

