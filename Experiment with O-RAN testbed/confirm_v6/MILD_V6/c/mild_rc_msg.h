/*
 * mild_rc_msg.h -- builds the E2SM-RC control request "Slice-level PRB quota" (control style 2, action 6)
 * for one UE (gNB-DU UE ID = gNB-CU UE F1AP ID). Used by mild_e2gw.c and by its encoder test.
 */
#ifndef MILD_RC_MSG_H
#define MILD_RC_MSG_H
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "../../../../src/util/e.h"
#include "../../../../src/sm/rc_sm/ie/rc_data_ie.h"
#include "../../../../src/sm/rc_sm/ie/ir/lst_ran_param.h"
#include "../../../../src/sm/rc_sm/ie/ir/ran_param_list.h"
#include "../../../../src/sm/rc_sm/ie/ir/ran_param_struct.h"

/* ---------------------------------------------------------------------- E2SM-RC slice-level PRB quota */
static seq_ran_param_t p_octets(uint32_t id, uint8_t const* v, size_t n)
{
  seq_ran_param_t p = {0};
  p.ran_param_id = id;
  p.ran_param_val.type = ELEMENT_KEY_FLAG_TRUE_RAN_PARAMETER_VAL_TYPE;
  p.ran_param_val.flag_true = ecalloc(1, sizeof(ran_parameter_value_t));
  p.ran_param_val.flag_true->type = OCTET_STRING_RAN_PARAMETER_VALUE;
  p.ran_param_val.flag_true->octet_str_ran.len = n;
  p.ran_param_val.flag_true->octet_str_ran.buf = ecalloc(n, 1);
  memcpy(p.ran_param_val.flag_true->octet_str_ran.buf, v, n);
  return p;
}

static seq_ran_param_t p_int(uint32_t id, int64_t v)
{
  seq_ran_param_t p = {0};
  p.ran_param_id = id;
  p.ran_param_val.type = ELEMENT_KEY_FLAG_FALSE_RAN_PARAMETER_VAL_TYPE;
  p.ran_param_val.flag_false = ecalloc(1, sizeof(ran_parameter_value_t));
  p.ran_param_val.flag_false->type = INTEGER_RAN_PARAMETER_VALUE;
  p.ran_param_val.flag_false->int_ran = v;
  return p;
}

/* a STRUCTURE parameter that owns the n parameters in items (items is consumed) */
static seq_ran_param_t p_struct(uint32_t id, size_t n, seq_ran_param_t const* items)
{
  seq_ran_param_t p = {0};
  p.ran_param_id = id;
  p.ran_param_val.type = STRUCTURE_RAN_PARAMETER_VAL_TYPE;
  p.ran_param_val.strct = ecalloc(1, sizeof(ran_param_struct_t));
  p.ran_param_val.strct->sz_ran_param_struct = n;
  p.ran_param_val.strct->ran_param_struct = ecalloc(n, sizeof(seq_ran_param_t));
  memcpy(p.ran_param_val.strct->ran_param_struct, items, n * sizeof(seq_ran_param_t));
  return p;
}

/* a LIST parameter with ONE item; the item is a RAN Parameter Structure holding the n parameters in items */
static seq_ran_param_t p_list1(uint32_t id, size_t n, seq_ran_param_t const* items)
{
  seq_ran_param_t p = {0};
  p.ran_param_id = id;
  p.ran_param_val.type = LIST_RAN_PARAMETER_VAL_TYPE;
  p.ran_param_val.lst = ecalloc(1, sizeof(ran_param_list_t));
  p.ran_param_val.lst->sz_lst_ran_param = 1;
  p.ran_param_val.lst->lst_ran_param = ecalloc(1, sizeof(lst_ran_param_t));
  ran_param_struct_t* s = &p.ran_param_val.lst->lst_ran_param[0].ran_param_struct;
  s->sz_ran_param_struct = n;
  s->ran_param_struct = ecalloc(n, sizeof(seq_ran_param_t));
  memcpy(s->ran_param_struct, items, n * sizeof(seq_ran_param_t));
  return p;
}

static int hex_bytes(const char* hex, uint8_t* out, size_t n)
{
  if (strlen(hex) != 2 * n) return 0;
  for (size_t i = 0; i < n; i++) {
    unsigned v;
    if (sscanf(hex + 2 * i, "%2x", &v) != 1) return 0;
    out[i] = (uint8_t)v;
  }
  return 1;
}

/* E2SM-RC "Slice-level PRB quota" (control style 2, action 6), in the layout srsRAN decodes (srsRAN_Project
 * 24.04 ... 25.10, lib/e2/e2sm/e2sm_rc/e2sm_rc_control_action_du_executor.cpp and its unit-test helper):
 *
 *   1 RRM Policy Ratio List (LIST, one item)
 *     > 2 RRM Policy Ratio Group (STRUCTURE)
 *         >> 3 RRM Policy (STRUCTURE)
 *              >>> 5 RRM Policy Member List (LIST, one item)
 *                    >>>> 6 RRM Policy Member (STRUCTURE): 7 PLMN Identity, 8 S-NSSAI (STRUCTURE: 9 SST, 10 SD)
 *         >> 11 Min PRB Policy Ratio, 12 Max PRB Policy Ratio, 13 Dedicated PRB Policy Ratio
 *
 * srsRAN skips ID 4 ("Parameter id 4 missing/skipped in TS") and reads EVERY element with the choice
 * "ElementKeyFlagFalse". MILD v2.0 numbered 1..12 (4 = member list) and used KeyFlagTrue for PLMN/SST/SD:
 * srsRAN then skipped the member list, wrote into an empty vector and never answered the control.
 *
 * The IDs below are the srsRAN defaults; mild_e2gw replaces them with the IDs the gNB itself advertises in
 * its E2SM-RC RAN function definition (matched by parameter name), so another E2 node layout still works. */
typedef struct {
  uint32_t list, group, policy, member_list, member, plmn, snssai, sst, sd, min, max, ded;
} mild_rc_ids_t;

static mild_rc_ids_t mild_rc_default_ids(void)
{
  mild_rc_ids_t d = {1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13};
  return d;
}

static seq_ran_param_t p_octets_f(uint32_t id, uint8_t const* v, size_t n)
{
  seq_ran_param_t p = p_octets(id, v, n);
  /* same value, choice ElementKeyFlagFalse: flag_true and flag_false are the same pointer in FlexRIC's union,
   * so only the choice tag changes */
  p.ran_param_val.type = ELEMENT_KEY_FLAG_FALSE_RAN_PARAMETER_VAL_TYPE;
  return p;
}

static rc_ctrl_req_data_t mild_prb_quota_req_ids(mild_rc_ids_t const* I, uint32_t f1ap, uint8_t const plmn[3], int sst,
                                                 int sd, int mn, int mx, int ded)
{
  uint8_t sst_b[1] = {(uint8_t)sst};
  uint8_t sd_b[3] = {(uint8_t)((sd >> 16) & 0xff), (uint8_t)((sd >> 8) & 0xff), (uint8_t)(sd & 0xff)};
  seq_ran_param_t snssai_items[2] = {p_octets_f(I->sst, sst_b, 1), p_octets_f(I->sd, sd_b, 3)};
  seq_ran_param_t member_items[2] = {p_octets_f(I->plmn, plmn, 3), p_struct(I->snssai, 2, snssai_items)};
  seq_ran_param_t member = p_struct(I->member, 2, member_items);
  seq_ran_param_t member_list = p_list1(I->member_list, 1, &member);
  seq_ran_param_t policy = p_struct(I->policy, 1, &member_list);
  seq_ran_param_t group_items[4] = {policy, p_int(I->min, mn), p_int(I->max, mx), p_int(I->ded, ded)};
  seq_ran_param_t group = p_struct(I->group, 4, group_items);
  seq_ran_param_t ratio_list = p_list1(I->list, 1, &group);

  rc_ctrl_req_data_t req = {0};
  req.hdr.format = FORMAT_1_E2SM_RC_CTRL_HDR;
  req.hdr.frmt_1.ue_id.type = GNB_DU_UE_ID_E2SM;
  req.hdr.frmt_1.ue_id.gnb_du.gnb_cu_ue_f1ap = f1ap;
  req.hdr.frmt_1.ue_id.gnb_du.ran_ue_id = NULL;
  req.hdr.frmt_1.ric_style_type = 2;
  req.hdr.frmt_1.ctrl_act_id = 6;
  req.hdr.frmt_1.ric_ctrl_decision = NULL;
  req.msg.format = FORMAT_1_E2SM_RC_CTRL_MSG;
  req.msg.frmt_1.sz_ran_param = 1;
  req.msg.frmt_1.ran_param = ecalloc(1, sizeof(seq_ran_param_t));
  req.msg.frmt_1.ran_param[0] = ratio_list;
  return req;
}

static rc_ctrl_req_data_t mild_prb_quota_req(uint32_t f1ap, uint8_t const plmn[3], int sst, int sd, int mn, int mx, int ded)
{
  mild_rc_ids_t d = mild_rc_default_ids();
  return mild_prb_quota_req_ids(&d, f1ap, plmn, sst, sd, mn, mx, ded);
}

#endif
