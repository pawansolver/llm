export type ConversationState =
	| 'idle'
	| 'listening'
	| 'transcribing'
	| 'thinking'
	| 'speaking'
	| 'error';

const transitions: Record<ConversationState, readonly ConversationState[]> = {
	idle: ['listening', 'thinking', 'error'],
	listening: ['idle', 'transcribing', 'thinking', 'speaking', 'error'],
	transcribing: ['idle', 'thinking', 'error'],
	thinking: ['idle', 'listening', 'speaking', 'error'],
	speaking: ['idle', 'listening', 'thinking', 'error'],
	error: ['idle', 'listening', 'thinking', 'speaking']
};

export const canTransitionConversation = (
	from: ConversationState,
	to: ConversationState
): boolean => from === to || transitions[from].includes(to);

export const transitionConversation = (
	from: ConversationState,
	to: ConversationState
): ConversationState => {
	if (!canTransitionConversation(from, to)) {
		throw new Error(`Invalid conversation state transition: ${from} -> ${to}`);
	}
	return to;
};

export const normalizeBrowserVoiceId = (value?: string | null): string =>
	(value ?? '')
		.trim()
		.toLocaleLowerCase()
		.replace(/^urn:moz-tts:/, '')
		.replace(/\s+/g, ' ');

export const selectBrowserVoice = (
	voices: readonly SpeechSynthesisVoice[],
	preferred?: string | null,
	language?: string | null
): SpeechSynthesisVoice | undefined => {
	const normalizedPreferred = normalizeBrowserVoiceId(preferred);
	const exact = voices.find(
		(voice) =>
			normalizeBrowserVoiceId(voice.voiceURI) === normalizedPreferred ||
			normalizeBrowserVoiceId(voice.name) === normalizedPreferred
	);
	if (exact) return exact;

	const normalizedLanguage = (language ?? '').trim().toLocaleLowerCase();
	const languageMatch = normalizedLanguage
		? voices.find((voice) => voice.lang.toLocaleLowerCase().startsWith(normalizedLanguage))
		: undefined;
	return languageMatch ?? voices.find((voice) => voice.default) ?? voices[0];
};
