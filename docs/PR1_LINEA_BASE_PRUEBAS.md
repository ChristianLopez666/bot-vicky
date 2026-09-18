# PR-1 · Línea base de pruebas (Vicky Redes)

**Fecha:** 2026-09-18 · **Base:** `main` en `b1978e1` (lo desplegado en `bot-vicky-redes`).

## Regla

El CI de `main` ya estaba en rojo antes de este PR: 19 pruebas fallaban en `b1978e1`
(el commit de ese deploy lo reconoce como "baseline previo"). El PR-1 se acepta
solo si:

1. el conjunto de pruebas que falla es **idéntico, nombre por nombre**, al de `main`;
2. todas las pruebas nuevas del PR pasan en **Python 3.11 y 3.13** (la matriz del CI).

Esta rama no corrige ni oculta ninguna de las 19. No se marcó ninguna con `skip`
ni `xfail`.

## Las 19 fallas preexistentes de `main` (b1978e1)

1. `tests/test_cierre_cortesia_post_propuesta.py::test_acuse_automatico_tras_la_propuesta_en_el_funnel_de_texto`
2. `tests/test_cierre_cortesia_post_propuesta.py::test_acuse_automatico_tras_terminar_el_flow_dinamico`
3. `tests/test_cierre_cortesia_post_propuesta.py::test_el_acuse_no_sale_si_el_cierre_no_se_pudo_entregar`
4. `tests/test_cierre_cortesia_post_propuesta.py::test_el_recordatorio_no_se_entrega_dos_veces_en_el_mismo_ciclo`
5. `tests/test_cierre_cortesia_post_propuesta.py::test_el_recordatorio_queda_armado_tras_el_acuse`
6. `tests/test_cierre_cortesia_post_propuesta.py::test_el_recordatorio_se_entrega_una_sola_vez_cuando_vence`
7. `tests/test_cierre_cortesia_post_propuesta.py::test_gracias_recibe_cortesia_sin_genero_con_invitacion_al_menu`
8. `tests/test_cierre_cortesia_post_propuesta.py::test_la_cortesia_no_se_repite_si_el_cliente_agradece_dos_veces`
9. `tests/test_cierre_cortesia_post_propuesta.py::test_la_oferta_del_menu_tambien_lleva_recordatorio_a_la_hora`
10. `tests/test_cierre_cortesia_post_propuesta.py::test_negativa_despues_de_la_cortesia_cierra_la_conversacion`
11. `tests/test_cierre_cortesia_post_propuesta.py::test_negativa_en_la_pregunta_de_horario_agradece_y_no_inventa_horario`
12. `tests/test_cierre_cortesia_post_propuesta.py::test_si_el_cliente_elige_horario_el_recordatorio_ya_no_existe`
13. `tests/test_cierre_cortesia_post_propuesta.py::test_si_el_cliente_responde_que_no_el_recordatorio_ya_no_existe`
14. `tests/test_cierre_cortesia_post_propuesta.py::test_tras_la_cortesia_una_segunda_negativa_tampoco_arma_nada`
15. `tests/test_cierre_cortesia_post_propuesta.py::test_un_mensaje_con_intencion_nueva_no_se_traga_como_negativa`
16. `tests/test_cierre_cortesia_post_propuesta.py::test_un_recordatorio_muy_atrasado_ya_no_se_entrega`
17. `tests/test_imss_commercial_experience_patch.py::test_closing_and_horario_in_a_single_bubble`
18. `tests/test_imss_visible_loan_proposal.py::test_gracias_after_successful_close_gets_courtesy_not_fallback`
19. `tests/test_imss_visible_loan_proposal.py::test_no_duplicate_responses_in_imss_flow`

## Pruebas nuevas del PR-1 (deben pasar en 3.11 y 3.13)

- `tests/test_p0_handsoff_2026_09_18.py`: 3 de caracterización del P0-1. Fallan en `b1978e1` y pasan aquí sin cambios.
- `tests/test_radar_circuito.py`: 6 del circuito persistente.
- `tests/test_radar_rechazo_sanitizado.py`: 24 de sanitizado (bearer token, API key, correo, URL con query, UUID, teléfono, HTML y texto plano; allowlist; huella HMAC; pestaña del circuito).

## Cómo se verificó

- Local: `python -m pytest -q` → conjunto de fallas comparado con `diff` contra `main`.
- CI (GitHub Actions, `tests.yml`, 3.11 y 3.13): la lista de `FAILED` de cada job se compara con la de arriba y se confirma que las pruebas nuevas figuran como `PASSED`. El resultado de cada corrida queda en la descripción del PR.
- Mutación: si se reintroduce texto libre en `detalle`, 8 pruebas de sanitizado fallan. Las pruebas sí detectan la fuga.
