/* test_rc_enc.c -- encodes the MILD "Slice-level PRB quota" control with FlexRIC's E2SM-RC v1.03 ASN.1
 * codec, decodes it back, compares, and prints the ASN.1 value (XER) for a visual check. */
#include "mild_rc_msg.h"
#include "../../../../src/sm/rc_sm/enc/rc_enc_asn.h"
#include "../../../../src/sm/rc_sm/dec/rc_dec_asn.h"
#include "../../../../src/sm/rc_sm/ie/asn/E2SM-RC-ControlMessage.h"
#include "../../../../src/sm/rc_sm/ie/asn/E2SM-RC-ControlHeader.h"
#include <assert.h>

/* the layout srsRAN decodes (see mild_rc_msg.h): IDs 1,2,3,5,6,7,8,9,10,11,12,13, every element KeyFlagFalse */
static seq_ran_param_t const* child(seq_ran_param_t const* p, size_t i, uint32_t want_id, int want_type)
{
  seq_ran_param_t const* c = NULL;
  if (p->ran_param_val.type == STRUCTURE_RAN_PARAMETER_VAL_TYPE && i < p->ran_param_val.strct->sz_ran_param_struct)
    c = &p->ran_param_val.strct->ran_param_struct[i];
  else if (p->ran_param_val.type == LIST_RAN_PARAMETER_VAL_TYPE && p->ran_param_val.lst->sz_lst_ran_param == 1 &&
           i < p->ran_param_val.lst->lst_ran_param[0].ran_param_struct.sz_ran_param_struct)
    c = &p->ran_param_val.lst->lst_ran_param[0].ran_param_struct.ran_param_struct[i];
  if (c == NULL || c->ran_param_id != want_id || (int)c->ran_param_val.type != want_type) {
    printf("layout: expected id %u type %d under id %u, got %s%u type %d\n", want_id, want_type, p->ran_param_id,
           c ? "" : "nothing ", c ? c->ran_param_id : 0, c ? (int)c->ran_param_val.type : -1);
    return NULL;
  }
  return c;
}

static int srsran_layout_ok(e2sm_rc_ctrl_msg_t const* m)
{
  const int S = STRUCTURE_RAN_PARAMETER_VAL_TYPE, L = LIST_RAN_PARAMETER_VAL_TYPE,
            F = ELEMENT_KEY_FLAG_FALSE_RAN_PARAMETER_VAL_TYPE;
  if (m->format != FORMAT_1_E2SM_RC_CTRL_MSG || m->frmt_1.sz_ran_param != 1) return 0;
  seq_ran_param_t const* l1 = &m->frmt_1.ran_param[0];
  if (l1->ran_param_id != 1 || l1->ran_param_val.type != LIST_RAN_PARAMETER_VAL_TYPE) return 0;
  seq_ran_param_t const* g2 = child(l1, 0, 2, S);
  if (!g2) return 0;
  seq_ran_param_t const* p3 = child(g2, 0, 3, S);
  if (!p3 || !child(g2, 1, 11, F) || !child(g2, 2, 12, F) || !child(g2, 3, 13, F)) return 0;
  seq_ran_param_t const* l5 = child(p3, 0, 5, L);
  if (!l5) return 0;
  seq_ran_param_t const* m6 = child(l5, 0, 6, S);
  if (!m6) return 0;
  seq_ran_param_t const* pl = child(m6, 0, 7, F);
  seq_ran_param_t const* n8 = child(m6, 1, 8, S);
  if (!pl || !n8 || pl->ran_param_val.flag_false->octet_str_ran.len != 3) return 0;
  seq_ran_param_t const* sst = child(n8, 0, 9, F);
  seq_ran_param_t const* sd = child(n8, 1, 10, F);
  if (!sst || !sd || sst->ran_param_val.flag_false->octet_str_ran.len != 1 || sd->ran_param_val.flag_false->octet_str_ran.len != 3)
    return 0;
  return 1;
}

int main(void)
{
  uint8_t plmn[3] = {0x00, 0xF1, 0x10};
  rc_ctrl_req_data_t r = mild_prb_quota_req(2, plmn, 1, 2, 5, 60, 0);
  byte_array_t h = rc_enc_ctrl_hdr_asn(&r.hdr);
  byte_array_t m = rc_enc_ctrl_msg_asn(&r.msg);
  printf("header  %zu bytes:", h.len); for (size_t i = 0; i < h.len; i++) printf(" %02x", h.buf[i]); printf("\n");
  printf("message %zu bytes:", m.len); for (size_t i = 0; i < m.len; i++) printf(" %02x", m.buf[i]); printf("\n");
  e2sm_rc_ctrl_hdr_t h2 = rc_dec_ctrl_hdr_asn(h.len, h.buf);
  e2sm_rc_ctrl_msg_t m2 = rc_dec_ctrl_msg_asn(m.len, m.buf);
  /* FlexRIC's eq_gnb_du_ue_id_e2sm dereferences the optional ran_ue_id: compare the header by hand */
  int ok_h = h2.format == FORMAT_1_E2SM_RC_CTRL_HDR && h2.frmt_1.ue_id.type == GNB_DU_UE_ID_E2SM &&
             h2.frmt_1.ue_id.gnb_du.gnb_cu_ue_f1ap == 2 && h2.frmt_1.ue_id.gnb_du.ran_ue_id == NULL &&
             h2.frmt_1.ric_style_type == 2 && h2.frmt_1.ctrl_act_id == 6;
  int ok_m = eq_e2sm_rc_ctrl_msg(&r.msg, &m2);
  int ok_l = srsran_layout_ok(&m2);
  printf("srsRAN layout (IDs 1,2,3,5,6,7,8,9,10,11,12,13; elements KeyFlagFalse): %s\n", ok_l ? "YES" : "NO");
  printf("round trip: header %s, message %s\n", ok_h ? "EQUAL" : "DIFFERENT", ok_m ? "EQUAL" : "DIFFERENT");
  E2SM_RC_ControlHeader_t* xh = NULL; E2SM_RC_ControlMessage_t* xm = NULL;
  aper_decode(NULL, &asn_DEF_E2SM_RC_ControlHeader, (void**)&xh, h.buf, h.len, 0, 0);
  aper_decode(NULL, &asn_DEF_E2SM_RC_ControlMessage, (void**)&xm, m.buf, m.len, 0, 0);
  xer_fprint(stdout, &asn_DEF_E2SM_RC_ControlHeader, xh);
  xer_fprint(stdout, &asn_DEF_E2SM_RC_ControlMessage, xm);
  return (ok_h && ok_m && ok_l) ? 0 : 1;
}
