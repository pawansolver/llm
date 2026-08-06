import { describe, expect, it } from 'vitest';
import {
	canTransitionConversation,
	normalizeBrowserVoiceId,
	selectBrowserVoice,
	transitionConversation
} from './voiceConversation';

const voice = (name: string, voiceURI: string, lang: string, isDefault = false) =>
	({ name, voiceURI, lang, default: isDefault }) as SpeechSynthesisVoice;

describe('voice conversation helpers', () => {
	it('allows the continuous conversation state path', () => {
		expect(canTransitionConversation('idle', 'listening')).toBe(true);
		expect(canTransitionConversation('listening', 'transcribing')).toBe(true);
		expect(canTransitionConversation('transcribing', 'thinking')).toBe(true);
		expect(canTransitionConversation('thinking', 'speaking')).toBe(true);
		expect(canTransitionConversation('speaking', 'listening')).toBe(true);
		expect(canTransitionConversation('speaking', 'thinking')).toBe(true);
		expect(canTransitionConversation('thinking', 'idle')).toBe(true);
		expect(canTransitionConversation('error', 'listening')).toBe(true);
	});

	it('rejects invalid state jumps', () => {
		expect(() => transitionConversation('idle', 'speaking')).toThrow(
			'Invalid conversation state transition'
		);
	});

	it('normalizes browser-specific voice identifiers', () => {
		expect(normalizeBrowserVoiceId(' URN:moz-tts:Microsoft   David ')).toBe('microsoft david');
	});

	it('selects by URI, name, language, then default', () => {
		const voices = [
			voice('English', 'urn:moz-tts:english', 'en-US', true),
			voice('Samantha', 'com.apple.samantha', 'en-GB')
		];
		expect(selectBrowserVoice(voices, 'COM.APPLE.SAMANTHA')?.name).toBe('Samantha');
		expect(selectBrowserVoice(voices, 'missing', 'en-GB')?.name).toBe('Samantha');
		expect(selectBrowserVoice(voices, 'missing', 'fr')?.name).toBe('English');
	});

	it('uses the declared default when no language preference exists', () => {
		const voices = [voice('First', 'first', 'fr-FR'), voice('Default', 'default', 'en-US', true)];
		expect(selectBrowserVoice(voices, null, null)?.name).toBe('Default');
		expect(selectBrowserVoice([], 'missing', 'en')).toBeUndefined();
	});
});
