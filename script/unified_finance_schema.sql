IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'finance')
    EXEC('CREATE SCHEMA finance');
GO

IF OBJECT_ID('finance.sync_runs', 'U') IS NULL
BEGIN
    CREATE TABLE finance.sync_runs (
        sync_run_id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        source_system            NVARCHAR(30) NOT NULL,
        process_name             NVARCHAR(100) NOT NULL,
        started_at               DATETIME2 NOT NULL CONSTRAINT DF_finance_sync_runs_started_at DEFAULT SYSUTCDATETIME(),
        finished_at              DATETIME2 NULL,
        status                   NVARCHAR(20) NOT NULL,
        rows_read                INT NULL,
        rows_upserted            INT NULL,
        rows_deleted_reloaded    INT NULL,
        error_message            NVARCHAR(MAX) NULL
    );
END
GO

IF OBJECT_ID('finance.sync_control', 'U') IS NULL
BEGIN
    CREATE TABLE finance.sync_control (
        process_name                NVARCHAR(100) NOT NULL PRIMARY KEY,
        source_system               NVARCHAR(30) NOT NULL,
        last_successful_sync        DATETIME2 NULL,
        last_attempt_sync           DATETIME2 NULL,
        last_begin_date_used        DATETIME2 NULL,
        last_end_date_used          DATETIME2 NULL,
        last_status                 NVARCHAR(20) NULL,
        last_error                  NVARCHAR(MAX) NULL,
        updated_at                  DATETIME2 NOT NULL CONSTRAINT DF_finance_sync_control_updated_at DEFAULT SYSUTCDATETIME()
    );
END
GO

/* =========================
   MERCADO PAGO
   ========================= */
IF OBJECT_ID('finance.mp_payment_items', 'U') IS NULL
BEGIN
    CREATE TABLE finance.mp_payment_items (
        item_row_id                 BIGINT IDENTITY(1,1) PRIMARY KEY,
        payment_id                  BIGINT NOT NULL,
        item_id                     NVARCHAR(100) NULL,
        category_id                 NVARCHAR(100) NULL,
        title                       NVARCHAR(MAX) NULL,
        description                 NVARCHAR(MAX) NULL,
        quantity                    DECIMAL(18,4) NULL,
        unit_price                  DECIMAL(18,2) NULL,
        picture_url                 NVARCHAR(MAX) NULL,
        created_at                  DATETIME2 NOT NULL CONSTRAINT DF_mp_payment_items_created_at DEFAULT SYSUTCDATETIME()
    );
END
GO

IF OBJECT_ID('finance.mp_payment_charges', 'U') IS NULL
BEGIN
    CREATE TABLE finance.mp_payment_charges (
        charge_row_id               BIGINT IDENTITY(1,1) PRIMARY KEY,
        payment_id                  BIGINT NOT NULL,
        charge_id                   NVARCHAR(100) NOT NULL,
        charge_type                 NVARCHAR(50) NULL,
        charge_name                 NVARCHAR(255) NULL,
        account_from                NVARCHAR(50) NULL,
        account_to                  NVARCHAR(50) NULL,
        amount_original             DECIMAL(18,2) NULL,
        amount_refunded             DECIMAL(18,2) NULL,
        base_amount                 DECIMAL(18,2) NULL,
        rate                        DECIMAL(18,6) NULL,
        reserve_id                  BIGINT NULL,
        client_id                   BIGINT NULL,
        tax_id                      BIGINT NULL,
        tax_status                  NVARCHAR(50) NULL,
        mov_detail                  NVARCHAR(100) NULL,
        mov_financial_entity        NVARCHAR(100) NULL,
        mov_type                    NVARCHAR(50) NULL,
        metadata_user_id            BIGINT NULL,
        metadata_source             NVARCHAR(100) NULL,
        charge_date_created         DATETIME2 NULL,
        charge_last_updated         DATETIME2 NULL,
        created_at                  DATETIME2 NOT NULL CONSTRAINT DF_mp_payment_charges_created_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_mp_payment_charges UNIQUE (payment_id, charge_id)
    );
END
GO

IF OBJECT_ID('finance.mp_payments', 'U') IS NULL
BEGIN
    CREATE TABLE finance.mp_payments (
        payment_id                  BIGINT NOT NULL PRIMARY KEY,
        collector_id                BIGINT NULL,
        payer_id                    BIGINT NULL,
        external_reference          NVARCHAR(255) NULL,
        order_id                    NVARCHAR(100) NULL,
        order_type                  NVARCHAR(50) NULL,
        status                      NVARCHAR(50) NULL,
        status_detail               NVARCHAR(100) NULL,
        operation_type              NVARCHAR(50) NULL,
        payment_method_id           NVARCHAR(50) NULL,
        payment_type_id             NVARCHAR(50) NULL,
        payment_method_type         NVARCHAR(50) NULL,
        issuer_id                   NVARCHAR(50) NULL,
        currency_id                 NVARCHAR(10) NULL,
        installments                INT NULL,
        transaction_amount          DECIMAL(18,2) NULL,
        transaction_amount_refunded DECIMAL(18,2) NULL,
        shipping_amount             DECIMAL(18,2) NULL,
        shipping_cost               DECIMAL(18,2) NULL,
        taxes_amount                DECIMAL(18,2) NULL,
        coupon_amount               DECIMAL(18,2) NULL,
        net_received_amount         DECIMAL(18,2) NULL,
        total_paid_amount           DECIMAL(18,2) NULL,
        installment_amount          DECIMAL(18,2) NULL,
        overpaid_amount             DECIMAL(18,2) NULL,
        description                 NVARCHAR(MAX) NULL,
        authorization_code          NVARCHAR(50) NULL,
        money_release_status        NVARCHAR(50) NULL,
        binary_mode                 BIT NULL,
        captured                    BIT NULL,
        live_mode                   BIT NULL,
        store_id                    NVARCHAR(50) NULL,
        pos_id                      NVARCHAR(50) NULL,
        notification_url            NVARCHAR(MAX) NULL,
        statement_descriptor        NVARCHAR(MAX) NULL,
        processing_mode             NVARCHAR(50) NULL,
        point_type                  NVARCHAR(50) NULL,
        point_unit                  NVARCHAR(50) NULL,
        point_sub_unit              NVARCHAR(50) NULL,
        point_branch                NVARCHAR(255) NULL,
        point_source                NVARCHAR(50) NULL,
        point_state_id              NVARCHAR(50) NULL,
        payer_email                 NVARCHAR(255) NULL,
        payer_identification_type   NVARCHAR(50) NULL,
        payer_identification_number NVARCHAR(100) NULL,
        card_first_six_digits       NVARCHAR(10) NULL,
        card_last_four_digits       NVARCHAR(10) NULL,
        cardholder_name             NVARCHAR(255) NULL,
        cardholder_ident_type       NVARCHAR(50) NULL,
        cardholder_ident_number     NVARCHAR(100) NULL,
        date_created                DATETIME2 NULL,
        date_approved               DATETIME2 NULL,
        date_last_updated           DATETIME2 NULL,
        money_release_date          DATETIME2 NULL,
        raw_json                    NVARCHAR(MAX) NULL,
        created_at                  DATETIME2 NOT NULL CONSTRAINT DF_mp_payments_created_at DEFAULT SYSUTCDATETIME(),
        updated_at                  DATETIME2 NOT NULL CONSTRAINT DF_mp_payments_updated_at DEFAULT SYSUTCDATETIME()
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_mp_payment_charges_payment')
    ALTER TABLE finance.mp_payment_charges ADD CONSTRAINT FK_mp_payment_charges_payment FOREIGN KEY (payment_id) REFERENCES finance.mp_payments(payment_id);
GO
IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_mp_payment_items_payment')
    ALTER TABLE finance.mp_payment_items ADD CONSTRAINT FK_mp_payment_items_payment FOREIGN KEY (payment_id) REFERENCES finance.mp_payments(payment_id);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_mp_payments_date_created' AND object_id = OBJECT_ID('finance.mp_payments'))
    CREATE INDEX IX_mp_payments_date_created ON finance.mp_payments(date_created);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_mp_payments_date_last_updated' AND object_id = OBJECT_ID('finance.mp_payments'))
    CREATE INDEX IX_mp_payments_date_last_updated ON finance.mp_payments(date_last_updated);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_mp_payments_external_reference' AND object_id = OBJECT_ID('finance.mp_payments'))
    CREATE INDEX IX_mp_payments_external_reference ON finance.mp_payments(external_reference);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_mp_payments_store_pos' AND object_id = OBJECT_ID('finance.mp_payments'))
    CREATE INDEX IX_mp_payments_store_pos ON finance.mp_payments(store_id, pos_id);
GO

/* =========================
   INTERBANKING
   ========================= */
IF OBJECT_ID('finance.ib_accounts', 'U') IS NULL
BEGIN
    CREATE TABLE finance.ib_accounts (
        account_cbu                 NVARCHAR(32) NOT NULL PRIMARY KEY,
        bank_number                 INT NULL,
        account_cuit                NVARCHAR(20) NULL,
        account_label               NVARCHAR(MAX) NULL,
        currency                    NVARCHAR(10) NULL,
        bank_name                   NVARCHAR(100) NULL,
        account_number              NVARCHAR(50) NULL,
        account_type                NVARCHAR(10) NULL,
        raw_json                    NVARCHAR(MAX) NULL,
        imported_at                 DATETIME2 NOT NULL CONSTRAINT DF_ib_accounts_imported_at DEFAULT SYSUTCDATETIME(),
        updated_at                  DATETIME2 NOT NULL CONSTRAINT DF_ib_accounts_updated_at DEFAULT SYSUTCDATETIME()
    );
END
GO

IF OBJECT_ID('finance.ib_balances', 'U') IS NULL
BEGIN
    CREATE TABLE finance.ib_balances (
        balance_id                  BIGINT IDENTITY(1,1) PRIMARY KEY,
        balance_hash                CHAR(64) NOT NULL,
        bank_number                 INT NULL,
        account_number              NVARCHAR(50) NULL,
        account_type                NVARCHAR(10) NULL,
        currency                    NVARCHAR(10) NULL,
        account_label               NVARCHAR(MAX) NULL,
        account_name                NVARCHAR(MAX) NULL,
        row_date                    DATETIME2 NULL,
        message                     NVARCHAR(MAX) NULL,
        countable_balance           DECIMAL(18,2) NULL,
        initial_operating_balance   DECIMAL(18,2) NULL,
        current_operating_balance   DECIMAL(18,2) NULL,
        projected_balance_24hs      DECIMAL(18,2) NULL,
        projected_balance_48hs      DECIMAL(18,2) NULL,
        operation_date              DATETIME2 NULL,
        day_balance                 DECIMAL(18,2) NULL,
        total_debits                DECIMAL(18,2) NULL,
        total_credits               DECIMAL(18,2) NULL,
        is_historical               BIT NULL,
        raw_json                    NVARCHAR(MAX) NULL,
        imported_at                 DATETIME2 NOT NULL CONSTRAINT DF_ib_balances_imported_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_ib_balances_hash UNIQUE (balance_hash)
    );
END
GO

IF OBJECT_ID('finance.ib_movements', 'U') IS NULL
BEGIN
    CREATE TABLE finance.ib_movements (
        movement_id                 BIGINT IDENTITY(1,1) PRIMARY KEY,
        movement_hash               CHAR(64) NOT NULL,
        account_cbu                 NVARCHAR(32) NULL,
        depositor_code              NVARCHAR(MAX) NULL,
        operation_code_ib           NVARCHAR(20) NULL,
        operation_code_bank         NVARCHAR(20) NULL,
        code_description_ib         NVARCHAR(MAX) NULL,
        customer_cuit               NVARCHAR(20) NULL,
        depositor_description       NVARCHAR(MAX) NULL,
        code_description_bank       NVARCHAR(MAX) NULL,
        amount                      DECIMAL(18,2) NULL,
        voucher_number              NVARCHAR(50) NULL,
        grouping_code_ib            NVARCHAR(50) NULL,
        branch_office_activity      NVARCHAR(50) NULL,
        process_date                DATETIME2 NULL,
        debit_credit_type           NVARCHAR(5) NULL,
        movement_type               NVARCHAR(20) NULL,
        source_account              NVARCHAR(50) NULL,
        associated_voucher          NVARCHAR(50) NULL,
        real_date_activity          DATETIME2 NULL,
        movement_date               DATETIME2 NULL,
        value_date                  DATETIME2 NULL,
        correlative_number          NVARCHAR(50) NULL,
        grouping_code_standard      NVARCHAR(50) NULL,
        code_description_standard   NVARCHAR(MAX) NULL,
        operation_code_standard     NVARCHAR(50) NULL,
        movement_source             NVARCHAR(12) NULL,
        raw_json                    NVARCHAR(MAX) NULL,
        imported_at                 DATETIME2 NOT NULL CONSTRAINT DF_ib_movements_imported_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_ib_movements_hash UNIQUE (movement_hash)
    );
END
GO

-- movement_source: distingue filas liquidadas ('anteriores') de las
-- provisionales del dia corriente ('dia'). Idempotente para DBs ya creadas.
IF COL_LENGTH('finance.ib_movements', 'movement_source') IS NULL
BEGIN
    ALTER TABLE finance.ib_movements ADD movement_source NVARCHAR(12) NULL;
END
GO

-- Indice filtrado: el poller borra/reescribe las filas 'dia' en cada ciclo.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_ib_movements_source_dia'
      AND object_id = OBJECT_ID('finance.ib_movements')
)
BEGIN
    CREATE INDEX IX_ib_movements_source_dia
        ON finance.ib_movements (movement_source)
        WHERE movement_source = 'dia';
END
GO

IF OBJECT_ID('finance.ib_transfers', 'U') IS NULL
BEGIN
    CREATE TABLE finance.ib_transfers (
        transfer_id                     BIGINT NOT NULL PRIMARY KEY,
        transaction_number              NVARCHAR(50) NULL,
        request_date                    DATETIME2 NULL,
        transfer_type_code              NVARCHAR(20) NULL,
        transfer_type_description       NVARCHAR(MAX) NULL,
        account_label                   NVARCHAR(MAX) NULL,
        amount                          DECIMAL(18,2) NULL,
        currency                        NVARCHAR(10) NULL,
        reference_number                NVARCHAR(50) NULL,
        lot_number                      NVARCHAR(50) NULL,
        payment_number                  NVARCHAR(50) NULL,
        status                          NVARCHAR(50) NULL,
        client                          NVARCHAR(MAX) NULL,
        statement_consolidated          NVARCHAR(10) NULL,
        unified_send                    NVARCHAR(10) NULL,
        direct_import                   NVARCHAR(10) NULL,
        same_owner                      NVARCHAR(10) NULL,
        internal_client_id              NVARCHAR(50) NULL,
        addenda                         NVARCHAR(MAX) NULL,
        transfer_comments               NVARCHAR(MAX) NULL,
        credit_account_customer_cuit    NVARCHAR(20) NULL,
        credit_account_account_cbu      NVARCHAR(32) NULL,
        credit_account_account_number   NVARCHAR(50) NULL,
        credit_account_currency         NVARCHAR(10) NULL,
        credit_account_account_type     NVARCHAR(10) NULL,
        credit_account_bank_number      INT NULL,
        credit_account_bank_name        NVARCHAR(MAX) NULL,
        credit_account_account_label    NVARCHAR(MAX) NULL,
        debit_account_customer_cuit     NVARCHAR(20) NULL,
        debit_account_account_cbu       NVARCHAR(32) NULL,
        debit_account_account_number    NVARCHAR(50) NULL,
        debit_account_currency          NVARCHAR(10) NULL,
        debit_account_account_type      NVARCHAR(10) NULL,
        debit_account_bank_number       INT NULL,
        debit_account_bank_name         NVARCHAR(MAX) NULL,
        debit_account_account_label     NVARCHAR(MAX) NULL,
        addenda_operation_numer         NVARCHAR(50) NULL,
        addenda_payment_receipt         NVARCHAR(50) NULL,
        addenda_amount                  DECIMAL(18,2) NULL,
        addenda_seller_tax_id           NVARCHAR(20) NULL,
        addenda_voucher_type            NVARCHAR(50) NULL,
        addenda_seller_name             NVARCHAR(MAX) NULL,
        addenda_community_code          NVARCHAR(50) NULL,
        addenda_seller_code             NVARCHAR(50) NULL,
        addenda_sale_point              NVARCHAR(50) NULL,
        addenda_request_date            DATETIME2 NULL,
        addenda_issue_date              DATETIME2 NULL,
        addenda_seller_company_name     NVARCHAR(MAX) NULL,
        addenda_voucher_number          NVARCHAR(50) NULL,
        addenda_due_date                DATETIME2 NULL,
        raw_json                        NVARCHAR(MAX) NULL,
        imported_at                     DATETIME2 NOT NULL CONSTRAINT DF_ib_transfers_imported_at DEFAULT SYSUTCDATETIME(),
        updated_at                      DATETIME2 NOT NULL CONSTRAINT DF_ib_transfers_updated_at DEFAULT SYSUTCDATETIME()
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_ib_transfers_transaction_number' AND object_id = OBJECT_ID('finance.ib_transfers'))
    CREATE UNIQUE INDEX UQ_ib_transfers_transaction_number ON finance.ib_transfers(transaction_number) WHERE transaction_number IS NOT NULL;
GO

IF OBJECT_ID('finance.ib_vouchers', 'U') IS NULL
BEGIN
    CREATE TABLE finance.ib_vouchers (
        voucher_id                      BIGINT IDENTITY(1,1) PRIMARY KEY,
        transfer_id                     BIGINT NOT NULL,
        request_date                    DATETIME2 NULL,
        transfer_type_description       NVARCHAR(MAX) NULL,
        transfer_type_code              NVARCHAR(20) NULL,
        network_number                  NVARCHAR(50) NULL,
        amount                          DECIMAL(18,2) NULL,
        currency                        NVARCHAR(10) NULL,
        validation_code                 NVARCHAR(100) NULL,
        total_amount                    DECIMAL(18,2) NULL,
        comments                        NVARCHAR(MAX) NULL,
        billing_company                 NVARCHAR(MAX) NULL,
        paying_customer                 NVARCHAR(MAX) NULL,
        debit_account_customer_cuit     NVARCHAR(20) NULL,
        debit_account_account_cbu       NVARCHAR(32) NULL,
        debit_account_taxpayer_cuit     NVARCHAR(20) NULL,
        debit_account_bank_number       INT NULL,
        debit_account_bank_name         NVARCHAR(MAX) NULL,
        debit_account_account_label     NVARCHAR(MAX) NULL,
        afip_concept_description        NVARCHAR(MAX) NULL,
        afip_control_code               NVARCHAR(50) NULL,
        afip_nro_formulario             NVARCHAR(50) NULL,
        afip_tax_description            NVARCHAR(MAX) NULL,
        afip_fee_number                 NVARCHAR(50) NULL,
        afip_pago_desc                  NVARCHAR(MAX) NULL,
        afip_provider_name              NVARCHAR(MAX) NULL,
        afip_concept_code               NVARCHAR(50) NULL,
        afip_tax_code                   NVARCHAR(50) NULL,
        afip_vep_number                 NVARCHAR(50) NULL,
        afip_fiscal_period              NVARCHAR(50) NULL,
        afip_provider_code              NVARCHAR(50) NULL,
        credit_account_customer_cuit    NVARCHAR(20) NULL,
        credit_account_account_cbu      NVARCHAR(32) NULL,
        credit_account_bank_number      INT NULL,
        credit_account_bank_name        NVARCHAR(MAX) NULL,
        credit_account_account_label    NVARCHAR(MAX) NULL,
        debit_account_voucher_number    NVARCHAR(50) NULL,
        billing_company_billing_company_cuit  NVARCHAR(20) NULL,
        billing_company_billing_company_name  NVARCHAR(MAX) NULL,
        billing_company_billing_account_name  NVARCHAR(MAX) NULL,
        billing_company_billing_seller        NVARCHAR(MAX) NULL,
        billing_company_billing_account_id    NVARCHAR(100) NULL,
        billing_company_due_date              DATETIME2 NULL,
        paying_customer_voucher_number  NVARCHAR(50) NULL,
        paying_customer_debit_bank      NVARCHAR(100) NULL,
        paying_customer_company_name    NVARCHAR(MAX) NULL,
        paying_customer_linkage_code    NVARCHAR(50) NULL,
        paying_customer_account_cuit    NVARCHAR(20) NULL,
        paying_customer_account_cbu     NVARCHAR(32) NULL,
        paying_customer_account_label   NVARCHAR(MAX) NULL,
        paying_customer_customer_cuit   NVARCHAR(20) NULL,
        raw_json                        NVARCHAR(MAX) NULL,
        imported_at                     DATETIME2 NOT NULL CONSTRAINT DF_ib_vouchers_imported_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_ib_vouchers_transfer UNIQUE (transfer_id)
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_ib_vouchers_transfer')
    ALTER TABLE finance.ib_vouchers ADD CONSTRAINT FK_ib_vouchers_transfer FOREIGN KEY (transfer_id) REFERENCES finance.ib_transfers(transfer_id);
GO

IF OBJECT_ID('finance.ib_extracts', 'U') IS NULL
BEGIN
    CREATE TABLE finance.ib_extracts (
        extract_id                   BIGINT IDENTITY(1,1) PRIMARY KEY,
        extract_hash                 CHAR(64) NOT NULL,
        statement_number             NVARCHAR(50) NULL,
        operation_date               DATETIME2 NULL,
        total_movements              INT NULL,
        opening_balance              DECIMAL(18,2) NULL,
        ending_balance               DECIMAL(18,2) NULL,
        operation_code_ib            NVARCHAR(20) NULL,
        operation_code_bank          NVARCHAR(20) NULL,
        code_description_ib          NVARCHAR(MAX) NULL,
        customer_cuit                NVARCHAR(20) NULL,
        depositor_description        NVARCHAR(MAX) NULL,
        code_description_bank        NVARCHAR(MAX) NULL,
        movement_date                DATETIME2 NULL,
        real_date_activity           DATETIME2 NULL,
        amount                       DECIMAL(18,2) NULL,
        voucher_number               NVARCHAR(50) NULL,
        grouping_code_ib             NVARCHAR(50) NULL,
        branch_office_activity       NVARCHAR(50) NULL,
        process_date                 DATETIME2 NULL,
        value_date                   DATETIME2 NULL,
        debit_credit_type            NVARCHAR(5) NULL,
        correlative_number           NVARCHAR(50) NULL,
        source_account               NVARCHAR(50) NULL,
        code_description_standard    NVARCHAR(MAX) NULL,
        operation_code_bank_standard NVARCHAR(50) NULL,
        raw_json                     NVARCHAR(MAX) NULL,
        imported_at                  DATETIME2 NOT NULL CONSTRAINT DF_ib_extracts_imported_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_ib_extracts_hash UNIQUE (extract_hash)
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ib_movements_source_account_process_date' AND object_id = OBJECT_ID('finance.ib_movements'))
    CREATE INDEX IX_ib_movements_source_account_process_date ON finance.ib_movements(source_account, process_date);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ib_extracts_source_account_operation_date' AND object_id = OBJECT_ID('finance.ib_extracts'))
    CREATE INDEX IX_ib_extracts_source_account_operation_date ON finance.ib_extracts(source_account, operation_date);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ib_balances_account_row_date' AND object_id = OBJECT_ID('finance.ib_balances'))
    CREATE INDEX IX_ib_balances_account_row_date ON finance.ib_balances(account_number, row_date);
GO

MERGE finance.sync_control AS tgt
USING (
    SELECT 'mercadopago_payments' AS process_name, 'MERCADOPAGO' AS source_system
    UNION ALL SELECT 'interbanking_accounts', 'INTERBANKING'
    UNION ALL SELECT 'interbanking_balances', 'INTERBANKING'
    UNION ALL SELECT 'interbanking_movements', 'INTERBANKING'
    UNION ALL SELECT 'interbanking_transfers', 'INTERBANKING'
    UNION ALL SELECT 'interbanking_vouchers', 'INTERBANKING'
    UNION ALL SELECT 'interbanking_extracts', 'INTERBANKING'
) AS src
ON tgt.process_name = src.process_name
WHEN NOT MATCHED THEN
    INSERT (process_name, source_system, last_successful_sync, last_attempt_sync, last_begin_date_used, last_end_date_used, last_status, last_error)
    VALUES (src.process_name, src.source_system, NULL, NULL, NULL, NULL, NULL, NULL);
GO
