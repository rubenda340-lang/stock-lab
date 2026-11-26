-- =============================================================================
-- StockLab — Esquema Multiusuario PostgreSQL para Supabase (Fase 2 + Fase 3)
-- =============================================================================
-- Ejecutar en: Supabase Dashboard -> SQL Editor -> New Query -> Run
--
-- Principios de seguridad:
--   1. Todas las tablas privadas llevan `user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE`.
--   2. Row-Level Security (RLS) activado en TODAS las tablas privadas.
--   3. Ningún usuario autenticado puede leer, insertar, modificar ni borrar
--      filas de otro usuario a nivel de motor PostgreSQL (`auth.uid() = user_id`).
--   4. Los datos globales de mercado (catálogo BME, ranking, predicciones ML)
--      se mantienen desacoplados para no alterar los contratos congelados.
-- =============================================================================

-- ── 1. Perfiles de usuario (sincronizados con auth.users) ───────────────────
CREATE TABLE IF NOT EXISTS public.profiles (
    id          UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    full_name   TEXT,
    avatar_url  TEXT,
    plan        TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'pro', 'admin')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "profiles_select_own" ON public.profiles;
CREATE POLICY "profiles_select_own" ON public.profiles
    FOR SELECT USING (auth.uid() = id);

DROP POLICY IF EXISTS "profiles_update_own" ON public.profiles;
CREATE POLICY "profiles_update_own" ON public.profiles
    FOR UPDATE USING (auth.uid() = id) WITH CHECK (auth.uid() = id);

-- ── 2. Watchlist privada por usuario ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.watchlist (
    user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    ticker          TEXT NOT NULL,
    fecha_adicion   DATE NOT NULL DEFAULT CURRENT_DATE,
    precio_adicion  DOUBLE PRECISION NOT NULL CHECK (precio_adicion > 0),
    precio_objetivo DOUBLE PRECISION CHECK (precio_objetivo IS NULL OR precio_objetivo > 0),
    motivo          TEXT,
    tipo_objetivo   TEXT NOT NULL DEFAULT 'COMPRA_EN_CAIDA'
                    CHECK (tipo_objetivo IN ('COMPRA_EN_CAIDA', 'COMPRA_EN_RUPTURA')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_user ON public.watchlist (user_id);

ALTER TABLE public.watchlist ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "watchlist_crud_own" ON public.watchlist;
CREATE POLICY "watchlist_crud_own" ON public.watchlist
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- ── 3. Cartera privada por usuario (Real y Simulada) ────────────────────────
CREATE TABLE IF NOT EXISTS public.cartera (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    tipo_cartera    TEXT NOT NULL DEFAULT 'SIMULADA' CHECK (tipo_cartera IN ('REAL', 'SIMULADA')),
    ticker          TEXT,
    empresa_nombre  TEXT,
    tipo            TEXT NOT NULL CHECK (tipo IN (
                        'COMPRA', 'VENTA', 'DIVIDENDO',
                        'INGRESO_EFECTIVO', 'RETIRADA_EFECTIVO', 'COMISION'
                    )),
    cantidad        DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (cantidad >= 0),
    precio_unitario DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (precio_unitario >= 0),
    importe_total   DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (importe_total >= 0),
    comision        DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (comision >= 0),
    fecha           DATE NOT NULL,
    notas           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cartera_user_tipo_fecha
    ON public.cartera (user_id, tipo_cartera, fecha, id);

ALTER TABLE public.cartera ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "cartera_crud_own" ON public.cartera;
CREATE POLICY "cartera_crud_own" ON public.cartera
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- ── 4. Preferencias de alertas por correo (Fase 3) ──────────────────────────
CREATE TABLE IF NOT EXISTS public.preferencias_alertas (
    user_id                  UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email_activo             BOOLEAN NOT NULL DEFAULT TRUE,
    email_destino            TEXT,
    avisar_wl_alcanzado      BOOLEAN NOT NULL DEFAULT TRUE,
    avisar_wl_cerca          BOOLEAN NOT NULL DEFAULT TRUE,
    umbral_wl_cerca_pct      DOUBLE PRECISION NOT NULL DEFAULT 3.0 CHECK (umbral_wl_cerca_pct BETWEEN 0.5 AND 25.0),
    avisar_cartera_real      BOOLEAN NOT NULL DEFAULT TRUE,
    avisar_cartera_simulada  BOOLEAN NOT NULL DEFAULT FALSE,
    umbral_ganancia_pct      DOUBLE PRECISION NOT NULL DEFAULT 15.0 CHECK (umbral_ganancia_pct BETWEEN 1.0 AND 500.0),
    umbral_perdida_pct       DOUBLE PRECISION NOT NULL DEFAULT -8.0 CHECK (umbral_perdida_pct BETWEEN -95.0 AND -1.0),
    umbral_intradia_pct      DOUBLE PRECISION NOT NULL DEFAULT 4.0 CHECK (umbral_intradia_pct BETWEEN 1.0 AND 50.0),
    resumen_diario           BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.preferencias_alertas ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "pref_alertas_crud_own" ON public.preferencias_alertas;
CREATE POLICY "pref_alertas_crud_own" ON public.preferencias_alertas
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- ── 5. Registro anti-spam de alertas enviadas por correo ────────────────────
CREATE TABLE IF NOT EXISTS public.alertas_enviadas_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    ticker      TEXT NOT NULL,
    tipo_alerta TEXT NOT NULL,
    fecha_dia   DATE NOT NULL DEFAULT CURRENT_DATE,
    detalle     TEXT,
    enviado_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, ticker, tipo_alerta, fecha_dia)
);

CREATE INDEX IF NOT EXISTS idx_alertas_log_user_fecha
    ON public.alertas_enviadas_log (user_id, fecha_dia DESC);

ALTER TABLE public.alertas_enviadas_log ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "alertas_log_select_own" ON public.alertas_enviadas_log;
CREATE POLICY "alertas_log_select_own" ON public.alertas_enviadas_log
    FOR SELECT USING (auth.uid() = user_id);

-- ── 6. Consumo diario de IA por usuario (protección de cuota Gemini) ────────
CREATE TABLE IF NOT EXISTS public.ia_consumo_diario (
    user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    fecha       DATE NOT NULL DEFAULT CURRENT_DATE,
    modelo      TEXT NOT NULL,
    peticiones  INTEGER NOT NULL DEFAULT 0 CHECK (peticiones >= 0),
    tokens      INTEGER NOT NULL DEFAULT 0 CHECK (tokens >= 0),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, fecha, modelo)
);

ALTER TABLE public.ia_consumo_diario ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "ia_consumo_crud_own" ON public.ia_consumo_diario;
CREATE POLICY "ia_consumo_crud_own" ON public.ia_consumo_diario
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- ── 7. Trigger automático al registrarse un usuario nuevo (Google o Email) ──
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.profiles (id, email, full_name, avatar_url)
    VALUES (
        NEW.id,
        COALESCE(NEW.email, ''),
        COALESCE(NEW.raw_user_meta_data->>'full_name', NEW.raw_user_meta_data->>'name', ''),
        COALESCE(NEW.raw_user_meta_data->>'avatar_url', NEW.raw_user_meta_data->>'picture', '')
    )
    ON CONFLICT (id) DO NOTHING;

    INSERT INTO public.preferencias_alertas (user_id, email_destino)
    VALUES (NEW.id, NEW.email)
    ON CONFLICT (user_id) DO NOTHING;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
