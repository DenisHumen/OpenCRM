import { ApiError } from "./api";
import type { TranslationKey } from "./i18n";

type T = (key: TranslationKey, params?: Record<string, string | number>) => string;

/**
 * Отказы сервера приходят с кодом и английской строкой (`errors.*`, одна на
 * всех), а плашка показывала строку как есть — русский экран говорил
 * «Invalid email or password». Здесь коды, которые человек получает с экрана;
 * отказ с подробностями в строке («Not enough stock for 2 item(s)», «SKU X is
 * already used») остаётся строкой сервера — подробность важнее слова.
 * Сторож: каждый код карты обязан существовать на сервере (`test_screens.py`).
 */
export const KODY_OSHIBOK: Record<string, TranslationKey> = {
  account_disabled: "errAccountDisabled",
  account_pending: "errAccountPending",
  accrual_reverted: "errAccrualReverted",
  act_finished: "errActFinished",
  act_is_empty: "errActIsEmpty",
  amount_from_lines: "errAmountFromLines",
  api_key_expired: "errApiKeyExpired",
  api_key_revoked: "errApiKeyRevoked",
  backup_bad_key: "errBackupBadKey",
  backup_busy: "errBackupBusy",
  backup_gone: "errBackupGone",
  backup_key_exists: "errBackupKeyExists",
  backup_key_missing: "errBackupKeyMissing",
  backup_not_ready: "errBackupNotReady",
  barcode_required: "errBarcodeRequired",
  board_has_no_files: "errBoardHasNoFiles",
  body_required: "errBodyRequired",
  cannot_change_own_role: "errCannotChangeOwnRole",
  cannot_delete_self: "errCannotDeleteSelf",
  cannot_modify_root: "errCannotModifyRoot",
  client_required: "errClientRequired",
  contact_required: "errContactRequired",
  deal_closed: "errDealClosed",
  deal_order_exists: "errDealOrderExists",
  deal_other_client: "errDealOtherClient",
  disk_full: "errDiskFull",
  document_finished: "errDocumentFinished",
  email_taken: "errEmailTaken",
  file_empty: "errFileEmpty",
  file_too_large: "errFileTooLarge",
  invalid_credentials: "errInvalidCredentials",
  invalid_email: "errInvalidEmail",
  item_required: "errItemRequired",
  last_open_stage: "errLastOpenStage",
  last_root: "errLastRoot",
  last_warehouse: "errLastWarehouse",
  line_exists: "errLineExists",
  line_name_required: "errLineNameRequired",
  lines_required: "errLinesRequired",
  login_rate_limited: "errLoginRateLimited",
  mail_account_inactive: "errMailAccountInactive",
  mail_password_missing: "errMailPasswordMissing",
  mail_send_failed: "errMailSendFailed",
  mail_sync_failed: "errMailSyncFailed",
  module_disabled: "errModuleDisabled",
  module_not_ready: "errModuleNotReady",
  name_required: "errNameRequired",
  name_too_long: "errNameTooLong",
  no_next_stage: "errNoNextStage",
  no_product_lines: "errNoProductLines",
  no_reservation: "errNoReservation",
  no_warehouse: "errNoWarehouse",
  not_a_sales_order: "errNotASalesOrder",
  not_a_waybill: "errNotAWaybill",
  not_active: "errNotActive",
  not_an_act: "errNotAnAct",
  not_an_order: "errNotAnOrder",
  not_authenticated: "errNotAuthenticated",
  not_enough_stock: "errNotEnoughStock",
  not_note_author: "errNotNoteAuthor",
  nothing_to_print: "errNothingToPrint",
  nothing_to_return: "errNothingToReturn",
  order_finished: "errOrderFinished",
  order_is_empty: "errOrderIsEmpty",
  order_not_closed: "errOrderNotClosed",
  order_not_new: "errOrderNotNew",
  password_change_required: "errPasswordChangeRequired",
  payment_needs_income: "errPaymentNeedsIncome",
  period_too_long: "errPeriodTooLong",
  permission_denied: "errPermissionDenied",
  pipeline_empty: "errPipelineEmpty",
  product_deleted: "errProductDeleted",
  product_has_stock: "errProductHasStock",
  quantity_not_positive: "errQuantityNotPositive",
  quantity_too_large: "errQuantityTooLarge",
  rate_limited: "errRateLimited",
  refund_needs_category: "errRefundNeedsCategory",
  refund_required: "errRefundRequired",
  return_is_empty: "errReturnIsEmpty",
  return_not_posted: "errReturnNotPosted",
  role_name_taken: "errRoleNameTaken",
  same_warehouse: "errSameWarehouse",
  service_has_no_stock: "errServiceHasNoStock",
  session_invalid: "errSessionInvalid",
  sku_required: "errSkuRequired",
  system_note_immutable: "errSystemNoteImmutable",
  telegram_not_configured: "errTelegramNotConfigured",
  telephony_not_configured: "errTelephonyNotConfigured",
  template_name_taken: "errTemplateNameTaken",
  title_required: "errTitleRequired",
  transfer_already_reverted: "errTransferAlreadyReverted",
  waybill_is_empty: "errWaybillIsEmpty",
  waybill_not_posted: "errWaybillNotPosted",
  weak_password: "errWeakPassword",
  wrong_password: "errWrongPassword",
  zero_quantity: "errZeroQuantity",
};

export function podpisOshibki(e: unknown, t: T): string {
  if (!(e instanceof ApiError)) return t("error");
  const key = KODY_OSHIBOK[e.code];
  if (key) return t(key);
  // Семейства без своей строки: «… не найден» и «слишком много …» — одно слово на всех.
  if (e.code.endsWith("_not_found")) return t("errNotFound");
  if (e.code.endsWith("_rate_limited") || e.code.endsWith("_flooded")) return t("errTooMany");
  return e.message;
}
