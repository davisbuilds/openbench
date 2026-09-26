// Antigravity CLI protobuf field map — descriptor-pinned.
//
// Source of truth: FileDescriptorProtos extracted from the Antigravity
// `language_server` binary via `protodump` (see
// docs/specs/baselines/antigravity-proto-fieldmap.md for provenance + regen).
// The Step envelope + step-kind taxonomy come from
// third_party/gemini_coder/proto/trajectory.proto; token fields from
// google/cloud/aiplatform/master/usage_metadata.proto (Google's stable public schema).
//
// NOT pinned here (private exa.cortex_pb payload internals — cortex.proto is
// present in the binary but not recoverable by protodump's scanner): the
// per-kind payload message fields and CortexStepMetadata's model/usage nesting.
// Those are fixture-verified in the decoder (Task 2), not guessed.

/** google.cloud.aiplatform.master.UsageMetadata — token counts (all int32). */
export const USAGE_METADATA_FIELDS = {
  promptTokenCount: 1,
  candidatesTokenCount: 2,
  totalTokenCount: 3,
  cachedContentTokenCount: 5,
  toolUsePromptTokenCount: 13,
  thoughtsTokenCount: 14,
} as const;

// Cache-inclusive invariant: Google's promptTokenCount INCLUDES cached tokens,
// so the store must subtract them (mirrors the Codex/OpenAI handling):
//   tokens_in    = promptTokenCount - cachedContentTokenCount
//   cache_read   = cachedContentTokenCount
//   tokens_out   = candidatesTokenCount (thoughtsTokenCount tracked separately)

// --- Real token accounting: CortexGeneratorMetadata (private exa/jetski proto,
// not descriptor-recoverable). Field numbers below are EMPIRICALLY PINNED against
// all 21 local conversation usage records (2026-07-01), not guessed:
//   - field 1 is constant per model (1016 gemini-pro-default / 1020 flash) => system prompt
//   - field 3 == field 9 + field 10 in every record => output = thinking + answer
//   - field 2 shrinks as field 5 grows across a session => 2 is non-cached input, 5 is cache
// See docs/specs/baselines/antigravity-proto-fieldmap.md "Real token accounting".

/** gen_metadata blob wrapper → CortexGeneratorMetadata at field 1. */
export const GEN_METADATA_WRAPPER_FIELD = 1;

/** CortexGeneratorMetadata (the per-generation record). */
export const GENERATOR_METADATA_FIELDS = {
  usage: 4, // the CortexUsage sub-message
  model: 19, // string, e.g. "gemini-pro-default"
  modelDisplay: 21, // string, e.g. "Gemini 3.1 Pro (High)"
} as const;

/** CortexUsage sub-message (empirically pinned). All int varints. */
export const CORTEX_USAGE_FIELDS = {
  systemPromptTokens: 1, // constant per model
  inputTokens: 2, // non-cached prompt
  outputTokens: 3, // total output (= thinking + answer)
  cachedTokens: 5, // cached prompt prefix (intermittent)
  thinkingTokens: 9, // reasoning tokens
  answerTokens: 10, // outputTokens - thinkingTokens
} as const;

/** gemini_coder.Step envelope (trajectory.proto). */
export const STEP_FIELDS = {
  type: 1,          // exa.cortex_pb.CortexStepType (enum; its own numbering, NOT the oneof field #)
  status: 4,        // exa.cortex_pb.CortexStepStatus
  metadata: 5,      // exa.cortex_pb.CortexStepMetadata (holds model + usage; internals fixture-verified)
  error: 31,        // exa.cortex_pb.CortexErrorDetails
  subtrajectory: 6, // gemini_coder.Trajectory
  taskDetails: 148, // gemini_coder.TaskDetails
} as const;

/**
 * Step-kind discriminator: the `oneof step` payload field number in gemini_coder.Step.
 * 120 kinds. Read the kind from whichever oneof field is PRESENT in a decoded
 * `step_payload` — NOT from the `steps.step_type` column (that is the CortexStepType
 * enum, a different numbering). Verified against real rows: step_type 14→user_input
 * (oneof 19), 15→planner_response (20), 23→checkpoint (30), 90→ephemeral_message
 * (103), 98→conversation_history (111).
 */
export const STEP_PAYLOAD_KINDS: Record<number, string> = {
  7: 'dummy',
  8: 'plan_input',
  9: 'mquery',
  10: 'code_action',
  11: 'git_commit',
  12: 'finish',
  13: 'grep_search',
  14: 'view_file',
  15: 'list_directory',
  16: 'compile',
  19: 'user_input',
  20: 'planner_response',
  21: 'file_breakdown',
  22: 'view_code_item',
  23: 'write_to_file',
  24: 'error_message',
  28: 'run_command',
  30: 'checkpoint',
  32: 'propose_code',
  34: 'find',
  35: 'search_knowledge_base',
  36: 'suggested_responses',
  37: 'command_status',
  38: 'memory',
  39: 'lookup_knowledge_base',
  40: 'read_url_content',
  41: 'view_content_chunk',
  42: 'search_web',
  43: 'retrieve_memory',
  47: 'mcp_tool',
  48: 'manager_feedback',
  49: 'tool_call_proposal',
  50: 'tool_call_choice',
  52: 'trajectory_choice',
  55: 'clipboard',
  58: 'view_file_outline',
  59: 'check_deploy_status',
  60: 'post_pr_review',
  62: 'list_resources',
  63: 'read_resource',
  64: 'lint_diff',
  65: 'find_all_references',
  66: 'brain_update',
  67: 'open_browser_url',
  68: 'run_extension_code',
  71: 'proposal_feedback',
  72: 'trajectory_search',
  73: 'execute_browser_javascript',
  74: 'list_browser_pages',
  75: 'capture_browser_screenshot',
  76: 'click_browser_pixel',
  77: 'read_terminal',
  78: 'capture_browser_console_logs',
  79: 'read_browser_page',
  80: 'browser_get_dom',
  85: 'code_search',
  86: 'browser_input',
  87: 'browser_move_mouse',
  88: 'browser_select_option',
  89: 'browser_scroll_up',
  90: 'browser_scroll_down',
  91: 'browser_click_element',
  92: 'browser_press_key',
  93: 'task_boundary',
  94: 'notify_user',
  95: 'code_acknowledgement',
  96: 'internal_search',
  97: 'browser_subagent',
  98: 'file_change',
  100: 'move',
  101: 'browser_scroll',
  102: 'knowledge_generation',
  103: 'ephemeral_message',
  104: 'generate_image',
  105: 'delete_directory',
  106: 'compile_applet',
  107: 'install_applet_dependencies',
  108: 'install_applet_package',
  109: 'browser_resize_window',
  110: 'browser_drag_pixel_to_pixel',
  111: 'conversation_history',
  112: 'knowledge_artifacts',
  113: 'send_command_input',
  114: 'system_message',
  115: 'wait',
  116: 'agency_tool_call',
  117: 'cider_agent_dummy',
  118: 'build_cleaner',
  119: 'blaze_build_targets',
  120: 'blaze_test_targets',
  121: 'set_up_firebase',
  122: 'moma',
  123: 'restart_dev_server',
  124: 'deploy_firebase',
  125: 'browser_mouse_wheel',
  126: 'lint_applet',
  127: 'shell_exec',
  129: 'ki_insertion',
  130: 'retrieve_content',
  131: 'critique',
  132: 'findings',
  134: 'browser_mouse_up',
  135: 'browser_mouse_down',
  136: 'workspace_api',
  137: 'browser_list_network_requests',
  138: 'browser_get_network_request',
  139: 'browser_refresh_page',
  140: 'generic',
  141: 'edit_notebook',
  142: 'write_blob',
  143: 'invoke_subagent',
  144: 'read_notebook',
  145: 'propose_ai_comments',
  146: 'start_code_review',
  149: 'set_up_cloudsql',
  150: 'execute_notebook',
  151: 'cloudsql_update_schema',
  152: 'rpc_action',
  153: 'cloudsql_execute_sql',
  154: 'ask_question',
};

