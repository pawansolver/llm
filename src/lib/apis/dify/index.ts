import { DIFY_API_BASE_URL } from '$lib/constants';

export const getBaseInstruction = async () => {
	try {
		const res = await fetch(`${DIFY_API_BASE_URL}/get-base-instruction`);
		if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
		return await res.json();
	} catch (err) {
		console.error('Error fetching base instruction from Dify:', err);
		return null;
	}
};

export const getSkillInstruction = async (skillName: string) => {
	try {
		const res = await fetch(`${DIFY_API_BASE_URL}/get-instruction`, {
			method: 'POST',
			headers: {
				'Content-Type': 'application/json'
			},
			body: JSON.stringify({ skill: skillName })
		});
		if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
		return await res.json();
	} catch (err) {
		console.error(`Error fetching skill ${skillName} from Dify:`, err);
		return null;
	}
};

export const checkDifyHealth = async () => {
	try {
		const res = await fetch(`${DIFY_API_BASE_URL}/health`);
		return res.ok;
	} catch (err) {
		console.error('Error checking Dify health:', err);
		return false;
	}
};
