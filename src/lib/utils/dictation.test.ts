import { describe, expect, it } from 'vitest';
import {
	appendTranscript,
	completeDictation,
	getAudioFileExtension,
	getSupportedRecordingMimeType,
	joinDictationText,
	normalizeVadSettings,
	shouldStopAfterSilence
} from './dictation';

describe('dictation helpers', () => {
	it('negotiates the first browser-supported recording format', () => {
		expect(getSupportedRecordingMimeType((type) => type === 'audio/mp4')).toBe('audio/mp4');
		expect(getSupportedRecordingMimeType(() => false)).toBeUndefined();
	});

	it('derives safe file extensions from recorder MIME types', () => {
		expect(getAudioFileExtension('audio/webm;codecs=opus')).toBe('webm');
		expect(getAudioFileExtension('audio/mpeg')).toBe('mp3');
		expect(getAudioFileExtension('')).toBe('webm');
	});

	it('adds dictation without overwriting an existing draft', () => {
		expect(joinDictationText('Existing draft', 'new words')).toBe('Existing draft new words');
		expect(joinDictationText('Existing draft\n', ' new words ')).toBe('Existing draft\nnew words');
		expect(joinDictationText('', ' new words ')).toBe('new words');
	});

	it('auto-sends the freshly completed transcript, not stale draft text', () => {
		expect(completeDictation('Existing draft', 'fresh words', true)).toEqual({
			text: 'Existing draft fresh words',
			shouldSubmit: true
		});
		expect(completeDictation('Existing draft', '   ', true)).toEqual({
			text: 'Existing draft',
			shouldSubmit: false
		});
	});

	it('accumulates final speech segments with stable spacing', () => {
		expect(appendTranscript('hello ', ' world ')).toBe('hello world');
		expect(appendTranscript('', 'hello')).toBe('hello');
	});

	it('clamps voice activity settings to safe limits', () => {
		expect(normalizeVadSettings({ enabled: true, silenceDurationMs: 10, threshold: 4 })).toEqual({
			enabled: true,
			silenceDurationMs: 500,
			threshold: 1
		});
	});

	it('stops only after voice followed by configured silence', () => {
		const base = {
			enabled: true,
			heardVoice: true,
			level: 0.01,
			threshold: 0.02,
			now: 3_000,
			lastVoiceAt: 1_000,
			silenceDurationMs: 2_000
		};
		expect(shouldStopAfterSilence(base)).toBe(true);
		expect(shouldStopAfterSilence({ ...base, heardVoice: false })).toBe(false);
		expect(shouldStopAfterSilence({ ...base, level: 0.03 })).toBe(false);
		expect(shouldStopAfterSilence({ ...base, now: 2_999 })).toBe(false);
		expect(shouldStopAfterSilence({ ...base, enabled: false })).toBe(false);
	});
});
