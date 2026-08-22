import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.openwebui.app',
  appName: 'fluAi',
  webDir: 'build',
  server: {
    // DO NOT set server.url here.
    //
    // When server.url is set to an http:// address, Capacitor navigates the
    // WebView directly to that HTTP URL. Android WebView then treats the page
    // as an insecure origin and blocks navigator.mediaDevices.getUserMedia()
    // (mic / camera) — causing "Permission Denied" in voice / audio mode.
    //
    // Without server.url, Capacitor serves the built web assets from its own
    // local HTTPS server (https://localhost). That IS a secure origin, so
    // getUserMedia works correctly.
    //
    // The backend IP (192.168.31.15:8080) is now hardcoded directly inside
    // src/lib/constants.ts (WEBUI_BASE_URL) so all API calls go to the right
    // place without needing a WebView redirect.
    androidScheme: 'https',   // Serve local assets over https:// (secure origin)
    cleartext: true,          // Allow outbound HTTP calls to the backend
  },
  android: {
    allowMixedContent: true   // Allow http://192.168.31.15:8080 API calls from the https origin
  }
};

export default config;
