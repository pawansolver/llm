import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.openwebui.app',
  appName: 'fluAi',
  webDir: 'build',
  server: {
    // When running in production (APK), connect to the local backend on PC
    // Make sure your phone and PC are on the same Wi-Fi network
    url: 'http://192.168.31.15:8080',
    cleartext: true, // Allow HTTP (non-HTTPS) connections on Android
    androidScheme: 'https' // Treat WebView as HTTPS (secure context) so mic/camera work
  },
  android: {
    // Allow loading HTTP content in the HTTPS WebView context (needed for mic/camera)
    allowMixedContent: true
  }
};

export default config;
