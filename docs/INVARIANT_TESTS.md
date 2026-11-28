# Safety invariants and the tests that try to break them (brief section 4)

Every invariant has at least one automated test. "Property" means Hypothesis throws
thousands of random event sequences at the code; "chaos" means a failure is injected
into the running engine. All run in CI on every push.

| # | Invariant | Tests |
|---|---|---|
| 1 | Never net short | `test_oms.py::test_sell_guard_refuses_to_go_short`, `test_oms.py::test_random_broker_events_never_break_invariants` (property), `test_db_invariants.py::test_negative_open_quantity_refused`, `test_paper_router.py::test_rejections` (paper refuses naked sells) |
| 2 | One working exit order | `test_oms.py::test_happy_path_modify_resting_stop_into_exit`, `::test_cancel_then_exit_when_broker_cannot_modify`, property test above, `test_db_invariants.py::test_one_working_exit_property` (DB-level) |
| 3 | Protect each fill within N s, else flatten | `test_oms.py::test_protective_stop_deadline_flattens`, `::test_rejected_stop_flattens`, `test_chaos.py::test_partial_fill_keeps_the_part_and_sizes_the_stop`, `test_engine_restart.py::test_missing_stop_after_restart_is_unmanaged_then_protected` |
| 4 | Chase gap-skipped stops | `test_oms.py::test_gap_skipped_stop_is_chased_then_alerts`, `test_paper_router.py::test_gap_through_stop_is_chased_until_filled_on_paper`, `test_chaos.py::test_gap_skipped_stop_is_chased_until_flat` |
| 5 | Each intent sent once (tag stored first, looked up after timeout) | `test_oms.py::test_uncertain_send_is_looked_up_before_any_retry`, `test_paper_router.py::test_lost_response_is_looked_up_not_resent`, `test_chaos.py::test_order_timeout_is_looked_up_not_doubled`, `test_engine_store.py::test_round_trip_is_stored_and_ledgered` (NEW stored before SENT), `test_engine_restart.py` (tags never reused after restart), `test_tags.py` |
| 6 | Fail closed on bad inputs | `test_risk_modes.py::test_every_bad_input_blocks_entries`, `test_engine.py::test_entries_blocked_by_time_guard_research_only_and_stale_data`, `test_chaos.py::test_feed_drops_mid_trade_no_entries_but_position_still_protected`, `::test_token_expiry_mid_session_blocks_entries_keeps_managing`, `::test_duplicate_and_out_of_order_ticks`, `test_engine_service.py::test_setup_warms_up_reconciles_and_listens` (no warm-up data: blocked) |
| 7 | Rebuild state from the broker after a restart | `test_engine_restart.py` (all four), `test_chaos.py::test_kill_minus_9_with_open_position_then_restart` (real `kill -9`) |
| 8 | Breakers persist, count unrealized, logged reset | `test_risk_modes.py::test_daily_loss_counts_unrealized_and_survives_restart`, `::test_trades_per_day_and_streak_across_days`, `::test_weekly_limit`, `::test_manual_reset_needs_the_phrase_and_is_logged`, `test_engine.py::test_signal_to_closed_trade_in_shadow_and_portfolio_books` (close recorded) |
| 9 | AI out of the order path | `test_ai_advisor.py::test_slow_ai_never_delays_the_order_and_times_out_to_slot2` (trade open while the AI is still "thinking"), `::test_no_keys_means_no_providers_and_nothing_breaks` |
| 10 | Real money hard to reach, easy to leave | `test_risk_modes.py::test_mode_resolution_exhaustively` (2,304 combinations), `::test_restart_is_paper_until_armed`, `::test_arming_two_step_today_and_this_process_only`, `::test_arming_code_expires_and_three_wrong_codes_reset`, `test_api.py::test_real_money_needs_a_password_and_a_passed_gate`, `test_engine_restart.py::test_restart_resumes_protected_positions_and_exits_on_target` (back unarmed) |
| 11 | Emergency flatten works even if the engine hangs | `test_engine.py::test_flatten_all_exits_and_stops_new_entries`, `test_api.py::test_flatten_and_stop_buttons`, `test_watchdog.py::test_arm_and_flatten_are_two_step`, `test_watchdog.py::test_chaos_engine_hangs_and_watchdog_flattens` (real hung process) |
| 12 | Rate-limit every API; orders <= 4/s together | `test_fyers_adapter.py::test_rate_limiter_per_second_and_minute`, `test_chaos.py::test_all_order_operations_share_one_rate_limit` |
| 13 | Time guards and 15:10 square-off (CNC exempt) | `test_risk_modes.py::test_time_guards`, `::test_square_off_spares_delivery`, `test_engine.py::test_square_off_at_1510` |
| 14 | No silent downgrade from LIVE | `test_risk_modes.py::test_options_never_fall_back_to_semi_mid_session`, `test_chaos.py::test_static_ip_flips_mid_session_live_stops_entries_and_alerts` |
| 15 | SEMI verified, not assumed | `test_semi.py` (all five) |
| 16 | One instance trades an account | `test_risk_modes.py::test_second_engine_cannot_start`, `::test_foreign_instance_orders_detected`, `::test_mode_resolution_exhaustively` (SEMI/LIVE only on an execution instance), `test_engine_restart.py::test_foreign_instance_order_stops_new_entries`, `test_tags.py` |
| 17 | No strategy/mode change while exposed | `test_risk_modes.py::test_strategy_changes_blocked_while_exposed`, `test_strategies_table.py::test_placeholders_cannot_be_switched_on` |
| 18 | Secrets never leave the host | `test_secrets_scan.py` (repo + fixtures scanned), `test_ai_advisor.py::test_snapshot_is_scrubbed_before_it_leaves`, `test_api.py::test_strategies_table_and_settings_never_leak_keys`, `test_api.py::test_password_hash_roundtrip` |

Review regressions (`tests/test_review_fixes.py`): lost reply on a rejected exit (inv 2/3),
late entry fill after close (inv 1/11), refused modify (inv 2), bot token never logged
(inv 18), non-text Telegram message (inv 11), isolated timer steps, exits made while the
engine was down booked into breakers (inv 7/8), adopted positions squared off (inv 7/13),
moved stops survive restart; `test_api.py::test_review11_foreign_host_and_origin_are_refused`;
`test_chaos.py::test_candle_gap_blocks_entries_for_fifteen_minutes` (inv 6);
`test_semi.py::test_late_confirm_after_expiry_is_recorded_then_exited` (inv 15).

## Other Phase 2 gate items

| Gate item | Test |
|---|---|
| feed drops mid-trade | `test_chaos.py::test_feed_drops_mid_trade_no_entries_but_position_still_protected` |
| duplicate / out-of-order ticks | `test_chaos.py::test_duplicate_and_out_of_order_ticks` |
| order timeout | `test_chaos.py::test_order_timeout_is_looked_up_not_doubled` |
| partial fill | `test_chaos.py::test_partial_fill_keeps_the_part_and_sizes_the_stop` |
| gap-skipped SL | `test_chaos.py::test_gap_skipped_stop_is_chased_until_flat` |
| token expiry mid-session | `test_chaos.py::test_token_expiry_mid_session_blocks_entries_keeps_managing` |
| kill -9 with an open position, then restart | `test_chaos.py::test_kill_minus_9_with_open_position_then_restart` |
| static-IP check flips mid-session | `test_chaos.py::test_static_ip_flips_mid_session_live_stops_entries_and_alerts` |
| foreign-instance order | `test_engine_restart.py::test_foreign_instance_order_stops_new_entries` |
| engine hang, /flatten via the watchdog | `test_watchdog.py::test_chaos_engine_hangs_and_watchdog_flattens` |
| memory soak within 1 GB | `python -m app.tools.soak --days 10` (results in `docs/PROGRESS.md`) |
