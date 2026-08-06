import { afterEach, describe, expect, it, vi } from 'vitest';
import { synthesizeOpenAISpeech, transcribeAudio } from '.';

describe('enterprise voice boundary integration', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it('keeps selected LLM routing independent from mocked STT and TTS providers', async () => {
		const requests: Array<[RequestInfo | URL, RequestInit | undefined]> = [];
		const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
			requests.push([url, init]);
			if (`${url}`.endsWith('/audio/transcriptions')) {
				return new Response(
					JSON.stringify({ text: 'fresh voice request', filename: 'recording.webm' }),
					{
						status: 200,
						headers: { 'Content-Type': 'application/json' }
					}
				);
			}
			if (`${url}`.endsWith('/audio/speech')) {
				return new Response(new Blob(['mock-audio'], { type: 'audio/mpeg' }), { status: 200 });
			}
			throw new Error(`Unexpected provider request: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		const selectedLlm = vi.fn(async (modelId: string, prompt: string) => {
			expect(modelId).toBe('selected-enterprise-llm');
			expect(prompt).toBe('fresh voice request');
			return 'mock assistant answer';
		});
		const audio = new File([new Uint8Array([1, 2, 3])], 'recording.webm', {
			type: 'audio/webm'
		});

		const transcript = await transcribeAudio('token', audio, 'en');
		const answer = await selectedLlm('selected-enterprise-llm', transcript.text);
		await synthesizeOpenAISpeech('token', 'alloy', answer);

		expect(requests).toHaveLength(2);
		expect(`${requests[0][0]}`).toContain('/audio/transcriptions');
		expect(`${requests[1][0]}`).toContain('/audio/speech');
		expect(requests.every(([url]) => !`${url}`.toLowerCase().includes('groq'))).toBe(true);

		const sttBody = requests[0][1]?.body;
		expect(sttBody).toBeInstanceOf(FormData);
		expect((sttBody as FormData).get('language')).toBe('en');
		expect((sttBody as FormData).has('model')).toBe(false);

		const ttsPayload = JSON.parse(`${requests[1][1]?.body}`);
		expect(ttsPayload).toEqual({ input: 'mock assistant answer', voice: 'alloy' });
		expect(JSON.stringify(ttsPayload)).not.toContain('selected-enterprise-llm');
	});
});
