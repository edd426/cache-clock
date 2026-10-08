CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, host_type TEXT, branch TEXT, summary TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, turn_index INTEGER NOT NULL, user_message TEXT, assistant_response TEXT, timestamp TEXT, UNIQUE(session_id, turn_index));
CREATE TABLE assistant_usage_events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, turn_index INTEGER, agent_id TEXT, parent_tool_call_id TEXT, model TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER, total_nano_aiu INTEGER, request_multiplier REAL, duration_ms INTEGER, time_to_first_token_ms INTEGER, inter_token_latency_ms INTEGER, initiator TEXT, api_endpoint TEXT, reasoning_effort TEXT, finish_reason TEXT, content_filter_triggered INTEGER, token_details_json TEXT, created_at TEXT);
INSERT INTO sessions (id, cwd) VALUES ('sess-a', '/placeholder/project-three'), ('sess-b', '/placeholder/project-four');
INSERT INTO turns (session_id, turn_index, user_message, timestamp) VALUES ('sess-a', 0, 'placeholder', '2026-01-02 11:00:05'), ('sess-b', 0, 'placeholder', '2026-01-02 12:00:00'), ('sess-b', 1, NULL, '2026-01-02 12:05:00');
INSERT INTO assistant_usage_events (session_id, agent_id, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, initiator, created_at) VALUES
 ('sess-a', NULL, 'model-c', 5000, 100, 4000, 500, 'user', '2026-01-02T11:00:09.000Z'),
 ('sess-a', 'agent-1', 'model-c', 800, 20, 0, 0, 'sub-agent', '2026-01-02T11:00:12.000Z'),
 ('sess-b', NULL, 'model-d', 2000, 50, 0, 2000, 'user', '2026-01-02 12:00:30');
