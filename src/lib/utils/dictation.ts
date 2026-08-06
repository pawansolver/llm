export const DICTATION_MIME_TYPES = [
	'audio/webm;codecs=opus',
	'audio/webm',
	'audio/mp4;codecs=mp4a.40.2',
	'audio/mp4',
	'audio/ogg;codecs=opus',
	'audio/ogg'
] as const;

export type DictationVadSettings = {
	enabled: boolean;
	silenceDurationMs: number;
	threshold: number;
};

export const getSupportedRecordingMimeType = (
	isTypeSupported: ((mimeType: string) => boolean) | undefined
): string | undefined => {
	if (!isTypeSupported) return undefined;
	return DICTATION_MIME_TYPES.find((mimeType) => isTypeSupported(mimeType));
};

export const getAudioFileExtension = (mimeType: string): string => {
	const subtype = mimeType.split('/')[1]?.split(';')[0]?.toLowerCase();
	if (!subtype) return 'webm';
	if (subtype === 'mpeg') return 'mp3';
	if (subtype === 'x-wav') return 'wav';
	return subtype;
};

export const joinDictationText = (draft: string, transcript: string): string => {
	const spokenText = transcript.trim();
	if (!spokenText) return draft;
	if (!draft) return spokenText;
	return `${draft}${/\s$/.test(draft) ? '' : ' '}${spokenText}`;
};

export const completeDictation = (
	draft: string,
	transcript: string,
	autoSend: boolean
): { text: string; shouldSubmit: boolean } => {
	const text = joinDictationText(draft, transcript);
	return { text, shouldSubmit: autoSend && Boolean(transcript.trim()) };
};

export const appendTranscript = (current: string, addition: string): string => {
	const next = addition.trim();
	if (!next) return current.trim();
	if (!current.trim()) return next;
	return `${current.trim()} ${next}`;
};

export const normalizeVadSettings = (value?: {
	enabled?: boolean;
	silenceDurationMs?: number;
	threshold?: number;
}): DictationVadSettings => ({
	enabled: value?.enabled ?? false,
	silenceDurationMs: Math.min(30_000, Math.max(500, value?.silenceDurationMs ?? 2_000)),
	threshold: Math.min(1, Math.max(0.001, value?.threshold ?? 0.02))
});

export const shouldStopAfterSilence = ({
	enabled,
	heardVoice,
	level,
	threshold,
	now,
	lastVoiceAt,
	silenceDurationMs
}: {
	enabled: boolean;
	heardVoice: boolean;
	level: number;
	threshold: number;
	now: number;
	lastVoiceAt: number;
	silenceDurationMs: number;
}): boolean => enabled && heardVoice && level < threshold && now - lastVoiceAt >= silenceDurationMs;
