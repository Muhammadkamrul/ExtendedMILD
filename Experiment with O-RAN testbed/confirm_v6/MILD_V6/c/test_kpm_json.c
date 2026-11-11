/* test_kpm_json.c -- feeds synthetic E2SM-KPM v3 indications (format 1 = cell, format 3 = 3 UEs; with and
 * without the optional measurement-name list; one "no value" record; NTP collection time) to the gateway's
 * indication callback and prints the JSON lines it emits. */
#define MILD_E2GW_TEST
#include "mild_e2gw.c"

static meas_info_format_1_lst_t* infos(const char** n, int k)
{
  meas_info_format_1_lst_t* m = calloc(k, sizeof(*m));
  for (int i = 0; i < k; i++) { m[i].meas_type.type = NAME_MEAS_TYPE; m[i].meas_type.name = cp_str_to_ba(n[i]); }
  return m;
}
static void fill(kpm_ind_msg_format_1_t* f, const char** names, int k, int with_names, double base, int novalue)
{
  f->meas_data_lst_len = 1;
  f->meas_data_lst = calloc(1, sizeof(meas_data_lst_t));
  f->meas_data_lst[0].meas_record_len = k;
  f->meas_data_lst[0].meas_record_lst = calloc(k, sizeof(meas_record_lst_t));
  for (int i = 0; i < k; i++) {
    meas_record_lst_t* r = &f->meas_data_lst[0].meas_record_lst[i];
    if (novalue && i == k - 1) { r->value = NO_VALUE_MEAS_VALUE; continue; }
    if (i % 2) { r->value = REAL_MEAS_VALUE; r->real_val = base + i + 0.25; }
    else { r->value = INTEGER_MEAS_VALUE; r->int_val = (uint32_t)(base + i); }
  }
  if (with_names) { f->meas_info_lst_len = k; f->meas_info_lst = infos(names, k); }
}
int main(void)
{
  const char* names[3] = {"DRB.UEThpDl", "RRU.PrbTotDl", "DRB.RlcSduDelayDl"};
  /* the subscription's action definition: the names when the gNB omits them */
  kpm_act_def_t ad = {0};
  ad.type = FORMAT_4_ACTION_DEFINITION;
  ad.frm_4.action_def_format_1.meas_info_lst_len = 3;
  ad.frm_4.action_def_format_1.meas_info_lst = infos(names, 3);
  uint64_t ntp = ((uint64_t)(1793390658ULL + 2208988800ULL) << 32) | 0x80000000ULL;

  sm_ag_if_rd_t rd = {0};
  rd.type = INDICATION_MSG_AGENT_IF_ANS_V0;
  rd.ind.type = KPM_STATS_V3_0;
  rd.ind.kpm.ind.hdr.type = FORMAT_1_INDICATION_HEADER;
  rd.ind.kpm.ind.hdr.kpm_ric_ind_hdr_format_1.collectStartTime = ntp;
  /* 1. cell level, names present */
  rd.ind.kpm.ind.msg.type = FORMAT_1_INDICATION_MESSAGE;
  fill(&rd.ind.kpm.ind.msg.frm_1, names, 3, 1, 100, 0);
  rd.ind.kpm.act_def = NULL;
  sm_cb_kpm(&rd);
  /* 2. three UEs, names omitted (taken from the action definition), last record "no value" */
  memset(&rd.ind.kpm.ind.msg, 0, sizeof(rd.ind.kpm.ind.msg));
  rd.ind.kpm.ind.msg.type = FORMAT_3_INDICATION_MESSAGE;
  kpm_ind_msg_format_3_t* f3 = &rd.ind.kpm.ind.msg.frm_3;
  f3->ue_meas_report_lst_len = 3;
  f3->meas_report_per_ue = calloc(3, sizeof(meas_report_per_ue_t));
  for (int u = 0; u < 3; u++) {
    f3->meas_report_per_ue[u].ue_meas_report_lst.type = GNB_DU_UE_ID_E2SM;
    f3->meas_report_per_ue[u].ue_meas_report_lst.gnb_du.gnb_cu_ue_f1ap = u;
    fill(&f3->meas_report_per_ue[u].ind_msg_format_1, names, 3, 0, 1000 * (u + 1), u == 2);
  }
  rd.ind.kpm.act_def = &ad;
  sm_cb_kpm(&rd);
  return 0;
}
