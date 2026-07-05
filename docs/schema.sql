--
-- PostgreSQL database dump
--

\restrict rBAqgKZmYjn4uCfwMSDjfHcKQndCPTHuyZ2DRlD4hG5j7GCwTjLLLI1aPfOL5rB

-- Dumped from database version 18.3 (Debian 18.3-1.pgdg12+1)
-- Dumped by pg_dump version 18.3 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: defective_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.defective_status AS ENUM (
    'PENDING',
    'READY',
    'COMPLETED'
);


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    user_id integer,
    action text NOT NULL,
    entity_type text NOT NULL,
    entity_id integer,
    details jsonb,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;


--
-- Name: defective_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.defective_items (
    id integer NOT NULL,
    pallet_no text NOT NULL,
    product_name text,
    sku text NOT NULL,
    qty integer NOT NULL,
    status public.defective_status DEFAULT 'PENDING'::public.defective_status,
    created_by integer,
    created_at timestamp with time zone DEFAULT now(),
    completed_by integer,
    completed_at timestamp with time zone,
    CONSTRAINT defective_items_qty_check CHECK ((qty > 0))
);


--
-- Name: defective_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.defective_items_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: defective_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.defective_items_id_seq OWNED BY public.defective_items.id;


--
-- Name: defective_parts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.defective_parts (
    id integer NOT NULL,
    defective_id integer,
    part_code text NOT NULL,
    part_name text,
    qty integer NOT NULL,
    CONSTRAINT defective_parts_qty_check CHECK ((qty > 0))
);


--
-- Name: defective_parts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.defective_parts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: defective_parts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.defective_parts_id_seq OWNED BY public.defective_parts.id;


--
-- Name: inventory_snapshot; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inventory_snapshot (
    part_code text NOT NULL,
    part_name text,
    on_hand_qty integer DEFAULT 0 NOT NULL,
    location text,
    source_updated_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id integer NOT NULL,
    telegram_id bigint,
    name text NOT NULL,
    role text NOT NULL,
    active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT users_role_check CHECK ((role = ANY (ARRAY['returns'::text, 'repair'::text, 'admin'::text])))
);


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: audit_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);


--
-- Name: defective_items id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.defective_items ALTER COLUMN id SET DEFAULT nextval('public.defective_items_id_seq'::regclass);


--
-- Name: defective_parts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.defective_parts ALTER COLUMN id SET DEFAULT nextval('public.defective_parts_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: defective_items defective_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.defective_items
    ADD CONSTRAINT defective_items_pkey PRIMARY KEY (id);


--
-- Name: defective_parts defective_parts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.defective_parts
    ADD CONSTRAINT defective_parts_pkey PRIMARY KEY (id);


--
-- Name: inventory_snapshot inventory_snapshot_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_snapshot
    ADD CONSTRAINT inventory_snapshot_pkey PRIMARY KEY (part_code);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_telegram_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_telegram_id_key UNIQUE (telegram_id);


--
-- Name: idx_audit_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_audit_entity ON public.audit_log USING btree (entity_type, entity_id);


--
-- Name: idx_audit_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_audit_time ON public.audit_log USING btree (created_at DESC);


--
-- Name: idx_def_pallet; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_def_pallet ON public.defective_items USING btree (pallet_no);


--
-- Name: idx_def_sku; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_def_sku ON public.defective_items USING btree (sku);


--
-- Name: idx_def_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_def_status ON public.defective_items USING btree (status);


--
-- Name: idx_dp_defective; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dp_defective ON public.defective_parts USING btree (defective_id);


--
-- Name: idx_dp_part; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dp_part ON public.defective_parts USING btree (part_code);


--
-- Name: idx_inv_part_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inv_part_code ON public.inventory_snapshot USING btree (part_code);


--
-- Name: audit_log audit_log_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: defective_items defective_items_completed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.defective_items
    ADD CONSTRAINT defective_items_completed_by_fkey FOREIGN KEY (completed_by) REFERENCES public.users(id);


--
-- Name: defective_items defective_items_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.defective_items
    ADD CONSTRAINT defective_items_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: defective_parts defective_parts_defective_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.defective_parts
    ADD CONSTRAINT defective_parts_defective_id_fkey FOREIGN KEY (defective_id) REFERENCES public.defective_items(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict rBAqgKZmYjn4uCfwMSDjfHcKQndCPTHuyZ2DRlD4hG5j7GCwTjLLLI1aPfOL5rB

