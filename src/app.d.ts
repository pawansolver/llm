// See https://kit.svelte.dev/docs/types#app
// for information about these interfaces
declare global {
	namespace App {
		// interface Error {}
		// interface Locals {}
		// interface PageData {}
		// interface Platform {}
	}

	// Vite define() globals — replaced at build time in vite.config.ts
	const APP_VERSION: string;
	const APP_BUILD_HASH: string;
}

export {};
