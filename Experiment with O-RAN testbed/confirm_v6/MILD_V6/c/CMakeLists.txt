# MILD on O-RAN: the E42 gateway shared by the three MILD xApps (built inside the FlexRIC source tree
# by `mild-oran build-xapps`, as examples/xApp/c/mild/).
add_executable(mild_e2gw
               mild_e2gw.c
               ../../../../src/util/alg_ds/alg/defer.c
              )
target_link_libraries(mild_e2gw PUBLIC e42_xapp -pthread -lsctp -ldl)

# encoder self-test (not needed at run time): mild-oran build-xapps runs it once
add_executable(mild_rc_enc_test test_rc_enc.c)
target_link_libraries(mild_rc_enc_test PRIVATE rc_sm_static -lm)
target_include_directories(mild_rc_enc_test PRIVATE ${CMAKE_SOURCE_DIR}/src/sm/rc_sm/ie/asn)

# indication-to-JSON self-test (not needed at run time)
add_executable(mild_kpm_json_test test_kpm_json.c ../../../../src/util/alg_ds/alg/defer.c)
target_link_libraries(mild_kpm_json_test PUBLIC e42_xapp -pthread -lsctp -ldl)
