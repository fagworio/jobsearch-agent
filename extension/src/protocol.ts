export const PROTOCOL_VERSION = 1 as const;

export type Command =
  | "HELLO"
  | "PING"
  | "GET_PAGE"
  | "INSPECT_FORM"
  | "FILL_FORM"
  | "READ_FORM"
  | "UPLOAD_ARTIFACT"
  | "GET_CHALLENGE_STATE"
  | "REQUEST_SUBMIT"
  | "GET_SUBMIT_RESULT";

export type Request = {
  version: typeof PROTOCOL_VERSION;
  request_id: string;
  type: Command;
  payload: Record<string, unknown>;
};

export type Response = {
  version: typeof PROTOCOL_VERSION;
  request_id: string;
  ok: boolean;
  result?: Record<string, unknown>;
  error?: string;
};

export function request(requestId: string, type: Command, payload: Record<string, unknown> = {}): Request {
  if (!requestId.trim()) throw new Error("request_id must not be empty");
  return { version: PROTOCOL_VERSION, request_id: requestId, type, payload };
}
