/*
 * mild_e2gw.c -- the E2 side of the three MILD xApps (FlexRIC v3.0.0, E2AP v2/v3, E2SM-KPM v3.00, E2SM-RC v1.03).
 *
 * Every MILD xApp (xKPM, xSLA, xRC) starts its OWN copy of this program. Each copy connects to the FlexRIC
 * nearRT-RIC over E42 (SCTP 127.0.0.1:36422) and is therefore a separate xApp at the RIC, with its own
 * xApp ID, its own subscriptions and its own control procedures. The xApp's application logic (queues,
 * models, policy engine) runs in Python and talks to this program through two pipes:
 *
 *   stdout : one JSON object per line (events: hello, ready, sub, unsub, ind, ctrl, nodes, error, bye).
 *            FlexRIC's own library also prints to stdout ("[xApp]: CONTROL ACK rx", "[xApp]: CONTROL FAILURE
 *            rx", ...); the Python side keeps the lines that start with '{' and uses the CONTROL ACK/FAILURE
 *            lines to classify each control procedure. stdout is line buffered so the order is preserved.
 *   stdin  : one command per line (plain words, no JSON parser needed in C):
 *              sub <style>                       add one more KPM subscription (style 1 or 4)
 *              unsub                             remove the most recently added extra subscription
 *              ctrl <id> <f1ap> <sst> <sd> <min> <max> <ded>
 *                                                E2SM-RC control, style 2 (Radio Resource Allocation Control),
 *                                                action 6 (Slice-level PRB quota) for the UE whose gNB-CU UE
 *                                                F1AP ID is <f1ap>: RRM policy for S-NSSAI (<sst>, <sd>) with
 *                                                min / max / dedicated PRB ratio in percent
 *              nodes                             print the connected E2 nodes and their RAN functions
 *              quit                              unsubscribe and exit (also on stdin EOF)
 *
 * Settings (environment; FlexRIC itself parses the command-line flags -c -p -d -n):
 *   MILD_ROLE        kpm | rc                                                 (default kpm)
 *   MILD_STYLES      KPM styles subscribed at start, e.g. "1,4" or "1"          (default "1,4")
 *   MILD_PERIOD_MS   KPM report period, ms (srsRAN minimum 1000)               (default 1000)
 *   MILD_WAIT_S      seconds to wait for an E2 node with the needed function   (default 30)
 *   MILD_PLMN        PLMN identity for the RRM policy member, 6 hex digits     (default 00F110 = 001/01)
 *
 * Indications are classified by their MESSAGE FORMAT (format 1 = E2 node / cell level, format 3 = one block
 * per UE), not by the subscription's style: the Appendix F logger labelled every row style=1.
 *
 * Exit codes: 0 normal, 2 no E2 node with the needed RAN function, 3 no subscription accepted,
 *             4 the E2 node disappeared (gNB stopped: the Python side restarts this program later),
 *             5 the E2 node did not answer a subscription, subscription delete or control (see below).
 *
 * Unanswered requests. FlexRIC v3.0.0's synchronous xApp API waits 5 + 1 s for the answer and, in a release
 * build (asserts compiled out), then returns "success" although nothing arrived; its pending-event timer keeps
 * firing and re-sends the E42 SETUP every 5 s for the rest of the process. A real answer arrives in a few ms, so
 * a call that took >= MILD_TIMEOUT_MS (4.5 s) is reported as a timeout (ok 0, "timeout":1), followed by
 * {"ev":"e2_timeout",...}, and the program exits with code 5: the session is not usable any more. An E2 node that
 * stops answering is wedged; the collector restarts the RAN.
 *
 * E2SM-RC parameter IDs. The "Slice-level PRB quota" message uses the IDs the gNB advertises in its E2SM-RC
 * RAN function definition (matched by name; event {"ev":"rc_params",...}); srsRAN's layout is the fallback.
 */
#include "../../../../src/xApp/e42_xapp_api.h"
#include "../../../../src/util/alg_ds/alg/defer.h"
#include "../../../../src/util/byte_array.h"
#include "../../../../src/util/e.h"
#include "../../../../src/util/ngran_types.h"
#include "mild_rc_msg.h"

#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define KPM_RF_ID 2
#define RC_RF_ID 3
#define MILD_TIMEOUT_MS 4500.0
#define MAX_NAMES 64
#define MAX_SUBS 32

static pthread_mutex_t out_mtx = PTHREAD_MUTEX_INITIALIZER;
static uint32_t period_ms = 1000;
static char node_str[128] = "unknown";
static volatile int stopping = 0;
static uint64_t n_ind1 = 0, n_ind3 = 0;

static double mono_s(void)
{
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec + t.tv_nsec / 1e9;
}

static double wall_s(void)
{
  struct timespec t;
  clock_gettime(CLOCK_REALTIME, &t);
  return t.tv_sec + t.tv_nsec / 1e9;
}

/* one complete line on stdout, never interleaved with another thread's line */
static void emit(const char* fmt, ...)
{
  va_list ap;
  pthread_mutex_lock(&out_mtx);
  va_start(ap, fmt);
  vfprintf(stdout, fmt, ap);
  va_end(ap);
  fputc('\n', stdout);
  fflush(stdout);
  pthread_mutex_unlock(&out_mtx);
}

/* collectStartTime: srsRAN sends a 64-bit NTP timestamp; FlexRIC agents send microseconds since 1970 */
static double collect_to_unix(uint64_t v)
{
  const uint64_t ntp_off = 2208988800ULL;
  if ((v >> 32) > ntp_off && (v >> 32) < 6000000000ULL)
    return (double)((v >> 32) - ntp_off) + (double)(v & 0xffffffffULL) / 4294967296.0;
  if (v > 100000000000000ULL) return (double)v / 1e6;
  if (v > 100000000000ULL) return (double)v / 1e3;
  return (double)v;
}

/* ------------------------------------------------------------------------------------------- KPM output */
typedef struct {
  char* p;
  size_t len, cap;
} sbuf_t;

static void sb_add(sbuf_t* b, const char* fmt, ...)
{
  va_list ap;
  for (;;) {
    va_start(ap, fmt);
    int n = vsnprintf(b->p + b->len, b->cap - b->len, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if ((size_t)n < b->cap - b->len) {
      b->len += (size_t)n;
      return;
    }
    b->cap = 2 * b->cap + (size_t)n + 64;
    b->p = realloc(b->p, b->cap);
    assert(b->p != NULL);
  }
}

static void meas_name(meas_type_t const* t, char* out, size_t n)
{
  if (t->type == NAME_MEAS_TYPE) {
    char* s = cp_ba_to_str(t->name);
    snprintf(out, n, "%s", s);
    free(s);
  } else {
    snprintf(out, n, "meas_id_%u", (unsigned)t->id);
  }
}

/* {"metric": value, ...} for one format-1 block; names from the indication or, when the gNB leaves the
 * optional name list out, from the subscription's action definition */
static void add_frm1(sbuf_t* b, kpm_ind_msg_format_1_t const* m, kpm_act_def_format_1_t const* req, int* incomplete)
{
  sb_add(b, "{");
  int first = 1;
  for (size_t j = 0; j < m->meas_data_lst_len; j++) {
    meas_data_lst_t const* d = &m->meas_data_lst[j];
    if (d->incomplete_flag != NULL && *d->incomplete_flag == TRUE_ENUM_VALUE) *incomplete = 1;
    for (size_t i = 0; i < d->meas_record_len; i++) {
      char name[128];
      if (m->meas_info_lst_len == d->meas_record_len)
        meas_name(&m->meas_info_lst[i].meas_type, name, sizeof(name));
      else if (req != NULL && req->meas_info_lst_len == d->meas_record_len)
        meas_name(&req->meas_info_lst[i].meas_type, name, sizeof(name));
      else
        snprintf(name, sizeof(name), "record_%zu", i);
      meas_record_lst_t const* r = &d->meas_record_lst[i];
      if (!first) sb_add(b, ",");
      first = 0;
      if (r->value == INTEGER_MEAS_VALUE)
        sb_add(b, "\"%s\":%u", name, r->int_val);
      else if (r->value == REAL_MEAS_VALUE)
        sb_add(b, "\"%s\":%.6g", name, r->real_val);
      else
        sb_add(b, "\"%s\":null", name);
    }
  }
  sb_add(b, "}");
}

static void sm_cb_kpm(sm_ag_if_rd_t const* rd)
{
  if (stopping || rd == NULL || rd->type != INDICATION_MSG_AGENT_IF_ANS_V0 || rd->ind.type != KPM_STATS_V3_0)
    return;
  double tm = mono_s(), tw = wall_s();
  kpm_ind_data_t const* ind = &rd->ind.kpm.ind;
  kpm_act_def_t const* ad = rd->ind.kpm.act_def;
  kpm_act_def_format_1_t const* req = NULL;
  if (ad != NULL && ad->type == FORMAT_4_ACTION_DEFINITION)
    req = &ad->frm_4.action_def_format_1;
  else if (ad != NULL && ad->type == FORMAT_1_ACTION_DEFINITION)
    req = &ad->frm_1;
  double tc = 0;
  if (ind->hdr.type == FORMAT_1_INDICATION_HEADER)
    tc = collect_to_unix(ind->hdr.kpm_ric_ind_hdr_format_1.collectStartTime);
  sbuf_t b = {.p = malloc(4096), .len = 0, .cap = 4096};
  assert(b.p != NULL);
  int incomplete = 0;
  if (ind->msg.type == FORMAT_1_INDICATION_MESSAGE) {
    sb_add(&b, "{\"ev\":\"ind\",\"fmt\":1,\"t_mono\":%.6f,\"t_wall\":%.6f,\"tc\":%.6f,\"m\":", tm, tw, tc);
    add_frm1(&b, &ind->msg.frm_1, req, &incomplete);
    n_ind1++;
  } else if (ind->msg.type == FORMAT_3_INDICATION_MESSAGE) {
    sb_add(&b, "{\"ev\":\"ind\",\"fmt\":3,\"t_mono\":%.6f,\"t_wall\":%.6f,\"tc\":%.6f,\"ues\":[", tm, tw, tc);
    size_t n_ue = ind->msg.frm_3.ue_meas_report_lst_len;
    for (size_t u = 0; u < n_ue; u++) {
      ue_id_e2sm_t const* id = &ind->msg.frm_3.meas_report_per_ue[u].ue_meas_report_lst;
      long f1ap = -1;
      char ids[64];
      if (id->type == GNB_DU_UE_ID_E2SM) {
        f1ap = (long)id->gnb_du.gnb_cu_ue_f1ap;
        snprintf(ids, sizeof(ids), "du_f1ap:%ld", f1ap);
      } else if (id->type == GNB_UE_ID_E2SM) {
        snprintf(ids, sizeof(ids), "amf_ngap:%" PRIu64, id->gnb.amf_ue_ngap_id);
      } else {
        snprintf(ids, sizeof(ids), "ue_type_%d", (int)id->type);
      }
      sb_add(&b, "%s{\"id\":\"%s\",\"f1ap\":%ld,\"m\":", u ? "," : "", ids, f1ap);
      add_frm1(&b, &ind->msg.frm_3.meas_report_per_ue[u].ind_msg_format_1, req, &incomplete);
      sb_add(&b, "}");
    }
    sb_add(&b, "]");
    n_ind3++;
  } else {
    free(b.p);
    emit("{\"ev\":\"error\",\"what\":\"indication message format %d not handled\"}", (int)ind->msg.type);
    return;
  }
  sb_add(&b, ",\"incomplete\":%d}", incomplete);
  emit("%s", b.p);
  free(b.p);
}

/* ------------------------------------------------------------------------------------- KPM subscriptions */
static label_info_lst_t no_label(void)
{
  label_info_lst_t l = {0};
  l.noLabel = ecalloc(1, sizeof(enum_value_e));
  *l.noLabel = TRUE_ENUM_VALUE;
  return l;
}

static kpm_act_def_format_1_t frm1_for(char names[][64], int n)
{
  kpm_act_def_format_1_t f = {0};
  f.meas_info_lst_len = n;
  f.meas_info_lst = ecalloc(n, sizeof(meas_info_format_1_lst_t));
  for (int i = 0; i < n; i++) {
    f.meas_info_lst[i].meas_type.type = NAME_MEAS_TYPE;
    f.meas_info_lst[i].meas_type.name = cp_str_to_ba(names[i]);
    f.meas_info_lst[i].label_info_lst_len = 1;
    f.meas_info_lst[i].label_info_lst = ecalloc(1, sizeof(label_info_lst_t));
    f.meas_info_lst[i].label_info_lst[0] = no_label();
  }
  f.gran_period_ms = period_ms;
  f.cell_global_id = NULL;
  f.meas_bin_range_info_lst_len = 0;
  f.meas_bin_info_lst = NULL;
  return f;
}

/* srsRAN reports every UE for any condition; S-NSSAI SST == 1 is the condition FlexRIC's own xApp encodes */
static test_info_lst_t sst_equals(int sst)
{
  test_info_lst_t t = {0};
  t.test_cond_type = S_NSSAI_TEST_COND_TYPE;
  t.S_NSSAI = TRUE_TEST_COND_TYPE;
  t.test_cond = ecalloc(1, sizeof(test_cond_e));
  *t.test_cond = EQUAL_TEST_COND;
  t.test_cond_value = ecalloc(1, sizeof(test_cond_value_t));
  t.test_cond_value->type = OCTET_STRING_TEST_COND_VALUE;
  t.test_cond_value->octet_string_value = ecalloc(1, sizeof(byte_array_t));
  t.test_cond_value->octet_string_value->len = 1;
  t.test_cond_value->octet_string_value->buf = ecalloc(1, sizeof(uint8_t));
  t.test_cond_value->octet_string_value->buf[0] = (uint8_t)sst;
  return t;
}

static ric_report_style_item_t const* find_style(kpm_ran_function_def_t const* d, int style)
{
  for (size_t i = 0; i < d->sz_ric_report_style_list; i++)
    if ((int)d->ric_report_style_list[i].report_style_type == style - 1) /* enum is 0-based */
      return &d->ric_report_style_list[i];
  return NULL;
}

static e2_node_arr_xapp_t nodes;
static e2_node_connected_xapp_t* node = NULL;     /* the E2 node we work with (the srsRAN DU) */
static kpm_ran_function_def_t const* kpm_def = NULL;
static int rc_ok = 0;
static int sub_handle[MAX_SUBS];
static int sub_style[MAX_SUBS];
static int n_sub = 0, n_base = 0;
static mild_rc_ids_t rc_ids;

/* the E2 node did not answer: report and leave (FlexRIC's session is unusable after an unanswered request) */
static void e2_timeout(const char* what, double ms)
{
  stopping = 1;
  emit("{\"ev\":\"e2_timeout\",\"what\":\"%s\",\"ms\":%.1f,\"node\":\"%s\"}", what, ms, node_str);
  fflush(stdout);
  _exit(5);
}

static int subscribe(int style)
{
  if (kpm_def == NULL || n_sub >= MAX_SUBS) return 0;
  ric_report_style_item_t const* it = find_style(kpm_def, style);
  if (it == NULL) {
    emit("{\"ev\":\"sub\",\"ok\":0,\"style\":%d,\"why\":\"style not advertised\"}", style);
    return 0;
  }
  char names[MAX_NAMES][64];
  int nn = 0;
  for (size_t i = 0; i < it->meas_info_for_action_lst_len && nn < MAX_NAMES; i++) {
    char* s = cp_ba_to_str(it->meas_info_for_action_lst[i].name);
    snprintf(names[nn++], 64, "%s", s);
    free(s);
  }
  if (nn == 0) {
    emit("{\"ev\":\"sub\",\"ok\":0,\"style\":%d,\"why\":\"no metrics advertised\"}", style);
    return 0;
  }
  kpm_sub_data_t sub = {0};
  sub.ev_trg_def.type = FORMAT_1_RIC_EVENT_TRIGGER;
  sub.ev_trg_def.kpm_ric_event_trigger_format_1.report_period_ms = period_ms;
  sub.sz_ad = 1;
  sub.ad = ecalloc(1, sizeof(kpm_act_def_t));
  if (style == 1) {
    sub.ad[0].type = FORMAT_1_ACTION_DEFINITION;
    sub.ad[0].frm_1 = frm1_for(names, nn);
  } else {
    sub.ad[0].type = FORMAT_4_ACTION_DEFINITION;
    sub.ad[0].frm_4.matching_cond_lst_len = 1;
    sub.ad[0].frm_4.matching_cond_lst = ecalloc(1, sizeof(matching_condition_format_4_lst_t));
    sub.ad[0].frm_4.matching_cond_lst[0].test_info_lst = sst_equals(1);
    sub.ad[0].frm_4.action_def_format_1 = frm1_for(names, nn);
  }
  double t0 = mono_s();
  sm_ans_xapp_t h = report_sm_xapp_api(&node->id, KPM_RF_ID, &sub, sm_cb_kpm);
  double ms = 1000.0 * (mono_s() - t0);
  free_kpm_sub_data(&sub);
  if (ms >= MILD_TIMEOUT_MS) {
    emit("{\"ev\":\"sub\",\"ok\":0,\"style\":%d,\"ms\":%.1f,\"timeout\":1,\"why\":\"no answer from the E2 node\"}",
         style, ms);
    e2_timeout("subscription", ms);
  }
  if (!h.success) {
    emit("{\"ev\":\"sub\",\"ok\":0,\"style\":%d,\"ms\":%.1f,\"why\":\"rejected\"}", style, ms);
    return 0;
  }
  sub_handle[n_sub] = h.u.handle;
  sub_style[n_sub] = style;
  n_sub++;
  emit("{\"ev\":\"sub\",\"ok\":1,\"style\":%d,\"metrics\":%d,\"n\":%d,\"ms\":%.1f}", style, nn, n_sub, ms);
  return 1;
}

static void unsubscribe_last(int allow_base)
{
  if (n_sub == 0 || (!allow_base && n_sub <= n_base)) {
    emit("{\"ev\":\"unsub\",\"ok\":0,\"n\":%d}", n_sub);
    return;
  }
  n_sub--;
  double t0 = mono_s();
  rm_report_sm_xapp_api(sub_handle[n_sub]);
  double ms = 1000.0 * (mono_s() - t0);
  if (ms >= MILD_TIMEOUT_MS) {
    emit("{\"ev\":\"unsub\",\"ok\":0,\"style\":%d,\"n\":%d,\"ms\":%.1f,\"timeout\":1}", sub_style[n_sub], n_sub, ms);
    e2_timeout("subscription delete", ms);
  }
  emit("{\"ev\":\"unsub\",\"ok\":1,\"style\":%d,\"n\":%d,\"ms\":%.1f}", sub_style[n_sub], n_sub, ms);
}

static void do_ctrl(char* args, uint8_t const plmn[3])
{
  long id, f1ap, sst, sd, mn, mx, ded;
  if (sscanf(args, "%ld %ld %ld %ld %ld %ld %ld", &id, &f1ap, &sst, &sd, &mn, &mx, &ded) != 7) {
    emit("{\"ev\":\"error\",\"what\":\"ctrl needs 7 integers\"}");
    return;
  }
  if (!rc_ok) {
    emit("{\"ev\":\"ctrl\",\"id\":%ld,\"ok\":0,\"why\":\"node offers no E2SM-RC\"}", id);
    return;
  }
  rc_ctrl_req_data_t req =
      mild_prb_quota_req_ids(&rc_ids, (uint32_t)f1ap, plmn, (int)sst, (int)sd, (int)mn, (int)mx, (int)ded);
  emit("{\"ev\":\"ctrl_tx\",\"id\":%ld}", id);
  double t0 = mono_s();
  sm_ans_xapp_t a = control_sm_xapp_api(&node->id, RC_RF_ID, &req);
  double ms = 1000.0 * (mono_s() - t0);
  free_rc_ctrl_req_data(&req);
  if (ms >= MILD_TIMEOUT_MS) {
    emit("{\"ev\":\"ctrl\",\"id\":%ld,\"ok\":0,\"timeout\":1,\"ms\":%.2f,\"f1ap\":%ld,\"min\":%ld,\"max\":%ld}", id, ms,
         f1ap, mn, mx);
    e2_timeout("control", ms);
  }
  /* FlexRIC v3.0.0 returns success for both CONTROL ACK and CONTROL FAILURE; the line it printed just
   * before ("[xApp]: CONTROL ACK rx" or "[xApp]: CONTROL FAILURE rx") tells them apart */
  emit("{\"ev\":\"ctrl\",\"id\":%ld,\"ok\":%d,\"ms\":%.2f,\"f1ap\":%ld,\"min\":%ld,\"max\":%ld}", id, a.success ? 1 : 0,
       ms, f1ap, mn, mx);
}

static void print_nodes(void)
{
  e2_node_arr_xapp_t a = e2_nodes_xapp_api();
  pthread_mutex_lock(&out_mtx);
  printf("{\"ev\":\"nodes\",\"n\":%u,\"nodes\":[", (unsigned)a.len);
  for (size_t i = 0; i < a.len; i++) {
    printf("%s{\"nb\":%u,\"rf\":[", i ? "," : "", a.n[i].id.nb_id.nb_id);
    for (size_t r = 0; r < a.n[i].len_rf; r++)
      printf("%s%u", r ? "," : "", a.n[i].rf[r].id);
    printf("]}");
  }
  printf("]}\n");
  fflush(stdout);
  pthread_mutex_unlock(&out_mtx);
  free_e2_node_arr_xapp(&a);
}

/* the E2SM-RC parameter IDs of "Slice-level PRB quota" as the gNB advertises them (matched by name) */
static void learn_rc_ids(seq_ctrl_act_2_t const* act)
{
  mild_rc_ids_t I = mild_rc_default_ids();
  int found = 0;
  for (size_t i = 0; i < act->sz_seq_assoc_ran_param; i++) {
    char* nm = cp_ba_to_str(act->assoc_ran_param[i].name);
    char lo[160];
    size_t k = 0;
    for (; nm[k] && k < sizeof(lo) - 1; k++) lo[k] = (char)((nm[k] >= 'A' && nm[k] <= 'Z') ? nm[k] + 32 : nm[k]);
    lo[k] = '\0';
    free(nm);
    uint32_t id = act->assoc_ran_param[i].id;
    uint32_t* dst = NULL;
    if (strcmp(lo, "rrm policy ratio list") == 0) dst = &I.list;
    else if (strcmp(lo, "rrm policy ratio group") == 0) dst = &I.group;
    else if (strcmp(lo, "rrm policy") == 0) dst = &I.policy;
    else if (strcmp(lo, "rrm policy member list") == 0) dst = &I.member_list;
    else if (strcmp(lo, "rrm policy member") == 0) dst = &I.member;
    else if (strstr(lo, "plmn") != NULL) dst = &I.plmn;
    else if (strstr(lo, "s-nssai") != NULL || strcmp(lo, "snssai") == 0) dst = &I.snssai;
    else if (strcmp(lo, "sst") == 0) dst = &I.sst;
    else if (strcmp(lo, "sd") == 0) dst = &I.sd;
    else if (strstr(lo, "min prb") != NULL) dst = &I.min;
    else if (strstr(lo, "max prb") != NULL) dst = &I.max;
    else if (strstr(lo, "dedicated prb") != NULL) dst = &I.ded;
    if (dst != NULL) {
      *dst = id;
      found++;
    }
  }
  const char* src = "srsran-default";
  if (found >= 10) {
    rc_ids = I;
    src = "gnb";
  }
  emit("{\"ev\":\"rc_params\",\"source\":\"%s\",\"advertised\":%zu,\"matched\":%d,\"ids\":{\"list\":%u,"
       "\"group\":%u,\"policy\":%u,\"member_list\":%u,\"member\":%u,\"plmn\":%u,\"snssai\":%u,\"sst\":%u,"
       "\"sd\":%u,\"min\":%u,\"max\":%u,\"ded\":%u}}",
       src, act->sz_seq_assoc_ran_param, found, rc_ids.list, rc_ids.group, rc_ids.policy, rc_ids.member_list,
       rc_ids.member, rc_ids.plmn, rc_ids.snssai, rc_ids.sst, rc_ids.sd, rc_ids.min, rc_ids.max, rc_ids.ded);
}

/* every 3 s: is our E2 node still connected to the RIC? (the gNB stopping removes it) */
static void* watch_node(void* arg)
{
  (void)arg;
  int missing = 0;
  while (!stopping) {
    sleep(3);
    if (stopping) break;
    e2_node_arr_xapp_t a = e2_nodes_xapp_api();
    int found = 0;
    for (size_t i = 0; i < a.len; i++)
      if (a.n[i].id.nb_id.nb_id == node->id.nb_id.nb_id) found = 1;
    free_e2_node_arr_xapp(&a);
    missing = found ? 0 : missing + 1;
    if (missing >= 2) {
      emit("{\"ev\":\"node_lost\",\"node\":\"%s\"}", node_str);
      fflush(stdout);
      _exit(4);
    }
  }
  return NULL;
}

#ifndef MILD_E2GW_TEST
int main(int argc, char* argv[])
{
  setvbuf(stdout, NULL, _IOLBF, 0);
  const char* role = getenv("MILD_ROLE") ? getenv("MILD_ROLE") : "kpm";
  const char* styles = getenv("MILD_STYLES") ? getenv("MILD_STYLES") : "1,4";
  if (getenv("MILD_PERIOD_MS")) period_ms = (uint32_t)atoi(getenv("MILD_PERIOD_MS"));
  if (period_ms < 100) period_ms = 1000;
  int wait_s = getenv("MILD_WAIT_S") ? atoi(getenv("MILD_WAIT_S")) : 30;
  uint8_t plmn[3] = {0x00, 0xF1, 0x10};
  if (getenv("MILD_PLMN") && !hex_bytes(getenv("MILD_PLMN"), plmn, 3)) {
    fprintf(stderr, "MILD_PLMN must be 6 hex digits\n");
    return 1;
  }
  int is_rc = strcmp(role, "rc") == 0;
  rc_ids = mild_rc_default_ids();
  emit("{\"ev\":\"hello\",\"role\":\"%s\",\"pid\":%d,\"styles\":\"%s\",\"period_ms\":%u}", role, (int)getpid(),
       is_rc ? "" : styles, period_ms);

  fr_args_t args = init_fr_args(argc, argv);
  init_xapp_api(&args); /* returns once connected to the nearRT-RIC (E42 setup) */

  int need = is_rc ? RC_RF_ID : KPM_RF_ID;
  for (int t = 0; t <= wait_s && node == NULL; t++) {
    nodes = e2_nodes_xapp_api();
    for (size_t i = 0; i < nodes.len && node == NULL; i++)
      for (size_t r = 0; r < nodes.n[i].len_rf; r++)
        if (nodes.n[i].rf[r].id == need || (is_rc && nodes.n[i].rf[r].id == KPM_RF_ID)) {
          node = &nodes.n[i];
          break;
        }
    if (node == NULL) {
      free_e2_node_arr_xapp(&nodes);
      sleep(1);
    }
  }
  if (node == NULL) {
    emit("{\"ev\":\"error\",\"what\":\"no E2 node with RAN function %d after %d s\"}", need, wait_s);
    while (try_stop_xapp_api() == false) usleep(1000);
    return 2;
  }
  snprintf(node_str, sizeof(node_str), "%03u-%0*u/%s/nb%u/du%" PRIu64, node->id.plmn.mcc, node->id.plmn.mnc_digit_len,
           node->id.plmn.mnc, get_ngran_name(node->id.type), node->id.nb_id.nb_id,
           node->id.cu_du_id ? *node->id.cu_du_id : 0);
  for (size_t r = 0; r < node->len_rf; r++) {
    if (node->rf[r].id == KPM_RF_ID && node->rf[r].defn.type == KPM_RAN_FUNC_DEF_E) kpm_def = &node->rf[r].defn.kpm;
    if (node->rf[r].id == RC_RF_ID && node->rf[r].defn.type == RC_RAN_FUNC_DEF_E) {
      e2sm_rc_func_def_t const* d = &node->rf[r].defn.rc;
      if (d->ctrl != NULL)
        for (size_t s = 0; s < d->ctrl->sz_seq_ctrl_style; s++) {
          seq_ctrl_style_t const* cs = &d->ctrl->seq_ctrl_style[s];
          for (size_t a = 0; a < cs->sz_seq_ctrl_act; a++) {
            char* nm = cp_ba_to_str(cs->seq_ctrl_act[a].name);
            emit("{\"ev\":\"rc_action\",\"style\":%u,\"action\":%u,\"name\":\"%s\"}", cs->style_type,
                 cs->seq_ctrl_act[a].id, nm);
            if (cs->style_type == 2 && cs->seq_ctrl_act[a].id == 6) {
              rc_ok = 1;
              learn_rc_ids(&cs->seq_ctrl_act[a]);
            }
            free(nm);
          }
        }
      else
        rc_ok = 1; /* no control list decoded: try anyway; the gNB answers ACK or FAILURE */
    }
  }
  emit("{\"ev\":\"ready\",\"role\":\"%s\",\"node\":\"%s\",\"kpm\":%d,\"rc\":%d}", role, node_str, kpm_def != NULL,
       rc_ok);

  if (!is_rc) {
    if (kpm_def == NULL) {
      emit("{\"ev\":\"error\",\"what\":\"node %s offers no E2SM-KPM\"}", node_str);
      while (try_stop_xapp_api() == false) usleep(1000);
      return 2;
    }
    if (strchr(styles, '1')) subscribe(1);
    if (strchr(styles, '4')) subscribe(4);
    n_base = n_sub;
    if (n_sub == 0) {
      emit("{\"ev\":\"error\",\"what\":\"no subscription accepted\"}");
      while (try_stop_xapp_api() == false) usleep(1000);
      return 3;
    }
  }
  pthread_t wt;
  pthread_create(&wt, NULL, watch_node, NULL);

  char line[512];
  while (fgets(line, sizeof(line), stdin) != NULL) {
    line[strcspn(line, "\r\n")] = '\0';
    if (strncmp(line, "sub ", 4) == 0)
      subscribe(atoi(line + 4));
    else if (strcmp(line, "unsub") == 0)
      unsubscribe_last(0);
    else if (strncmp(line, "ctrl ", 5) == 0)
      do_ctrl(line + 5, plmn);
    else if (strcmp(line, "nodes") == 0)
      print_nodes();
    else if (strcmp(line, "quit") == 0)
      break;
    else if (line[0] != '\0')
      emit("{\"ev\":\"error\",\"what\":\"unknown command\"}");
  }
  stopping = 1;
  while (n_sub > 0) unsubscribe_last(1);
  while (try_stop_xapp_api() == false) usleep(1000);
  emit("{\"ev\":\"bye\",\"ind_fmt1\":%" PRIu64 ",\"ind_fmt3\":%" PRIu64 "}", n_ind1, n_ind3);
  return 0;
}
#endif
